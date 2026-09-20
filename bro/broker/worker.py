"""Worker-owned supervision for spawned, job, and expected broker missions."""

import asyncio
import shutil
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from bro.base import log
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag, Talk
from bro.broker.job import CommandJob, record_status
from bro.broker.runtime import Peer, Runtime
from bro.broker.spawn import ChildHandle, LaunchSpec

LAUNCH_TIMEOUT = 1800.0
_DRAIN_TIMEOUT = 2.0


class Timer(Protocol):
  def cancel(self) -> None: ...


Scheduler = Callable[[float, Callable[[], None]], Timer]


def call_later(seconds: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
  return asyncio.get_running_loop().call_later(seconds, callback)


def job_peer(quest: str) -> Peer:
  return f'job:{quest}'


@dataclass(frozen=True)
class DeathReport:
  reason: str
  error: Optional[str] = None
  exit_code: Optional[int] = None
  output_tail: Optional[str] = None


class WorkerListener(Protocol):
  def on_worker_bound(self, worker: 'Worker', peer: Peer) -> None: ...
  def on_worker_ready(self, worker: 'Worker') -> None: ...
  def on_worker_message(self, worker: 'Worker', message: Message, *, host_worker: bool) -> None: ...
  def on_worker_death(self, worker: 'Worker', report: DeathReport) -> None: ...


class JobOutput(Protocol):
  def open(self) -> Path: ...

  async def collect(self, directory: Path, context: Any, requester: Peer) -> dict: ...


class Worker:
  """Shared wait-task, two-phase deadline, and teardown ownership."""

  def __init__(
    self,
    runtime: Runtime,
    listener: WorkerListener,
    quest: str,
    *,
    timeout: Optional[float],
    launch_timeout: Optional[float] = LAUNCH_TIMEOUT,
    schedule: Scheduler = call_later,
  ):
    self.runtime = runtime
    self.listener = listener
    self.quest = quest
    self.timeout = timeout
    self.peer: Optional[Peer] = None
    self._launch_timeout = launch_timeout
    self._schedule = schedule
    self._task: Optional[asyncio.Task[None]] = None
    self._timer: Optional[Timer] = None
    self._started = False
    self._end_reason: Optional[str] = None
    self._finished = False
    self._stopping = False
    self._pending_messages: list[Message] = []
    self._cancel_after_kill = False

  def begin(self) -> None:
    if self._task is not None:
      raise RuntimeError('worker already begun')
    self._task = asyncio.create_task(self._run())
    self._task.add_done_callback(self._task_done)
    self._arm(self._launch_timeout)

  async def _run(self) -> None:
    raise NotImplementedError

  def _bind(self, peer: Peer) -> None:
    if self.peer is not None:
      raise RuntimeError('worker already bound')
    self.peer = peer
    self.listener.on_worker_bound(self, peer)

  def _mark_started(self) -> None:
    if self._started:
      return
    self._started = True
    self._arm(self.timeout)
    self.listener.on_worker_message(self, brotocol.mark(self.quest, 'started'), host_worker=True)
    pending = self._pending_messages
    self._pending_messages = []
    for message in pending:
      self.listener.on_worker_message(self, message, host_worker=False)

  def settle(self) -> None:
    """Disarm the mission deadline after its result without ending supervision."""
    self._cancel_timer()

  def _arm(self, seconds: Optional[float]) -> None:
    self._cancel_timer()
    if seconds is not None:
      self._timer = self._schedule(seconds, self._deadline)

  def _deadline(self) -> None:
    self._timer = None
    self.end('timeout')

  def end(self, reason: str) -> None:
    """Kill the worker; the death it then reports carries `reason`."""
    if self._finished or self._stopping or self._end_reason is not None:
      return
    self._end_reason = reason
    self._cancel_timer()
    self._end_now()

  @property
  def ending(self) -> bool:
    """Whether an end was accepted and the death carrying it is still to be reported."""
    return self._end_reason is not None and not self._finished

  def _reason_or(self, default: str) -> str:
    """The accepted end reason, or `default` when no end was accepted."""
    return self._end_reason if self._end_reason is not None else default

  def _end_now(self) -> None:
    if self._started:
      asyncio.create_task(self._kill())
      return
    if self._task is None:
      raise RuntimeError('worker ended before it began')
    self._task.cancel()

  async def _kill(self) -> None:
    raise NotImplementedError

  def _finish(self, report: DeathReport) -> None:
    if self._finished or self._stopping:
      return
    self._finished = True
    self._cancel_timer()
    self.listener.on_worker_death(self, report)

  async def stop(self) -> None:
    if self._stopping:
      return
    self._stopping = True
    self._cancel_timer()
    await self._kill()
    if self._task is not None and self._task is not asyncio.current_task():
      if not self._started or self._cancel_after_kill:
        self._task.cancel()
      await asyncio.gather(self._task, return_exceptions=True)
    if self.peer is not None and not self.peer.startswith('job:'):
      await self.runtime.close(self.peer)

  def _cancel_timer(self) -> None:
    if self._timer is not None:
      self._timer.cancel()
      self._timer = None

  def _task_done(self, task: asyncio.Task[None]) -> None:
    if self._stopping:
      return
    if task.cancelled():
      if self._end_reason is not None:
        self._finish(DeathReport(self._end_reason))
      return
    error = task.exception()
    if error is not None and not self._finished:
      log.warning('broker worker %s failed: %r', self.quest, error)
      default = 'launch' if not self._started else 'exit'
      self._finish(DeathReport(self._reason_or(default), error=str(error)))

  def on_connect(self) -> None:
    pass

  def on_message(self, message: Message) -> None:
    if message.type not in (Tag.REQUEST, Tag.MESSAGE) and message.request_id != self.quest:
      log.warning(
        'broker worker %s refused %r for mission %s',
        self.quest,
        message.type,
        message.request_id,
      )
      return
    if self._started:
      self.listener.on_worker_message(self, message, host_worker=False)
    else:
      self._pending_messages.append(message)

  def on_disconnect(self) -> None:
    pass


async def _own[T](work: Coroutine[Any, Any, T]) -> tuple[T, bool]:
  """Await `work` to completion even when the awaiting task is cancelled meanwhile.

  The flag says whether that happened; the caller settles what completed, then re-raises."""
  task = asyncio.create_task(work)
  cancelled = False
  while True:
    try:
      return await asyncio.shield(task), cancelled
    except asyncio.CancelledError:
      cancelled = True
      if task.cancelled():
        raise


async def _owned_launch(launch: Coroutine[Any, Any, ChildHandle]) -> ChildHandle:
  handle, cancelled = await _own(launch)
  if not cancelled:
    return handle
  await handle.kill()
  await handle.wait()
  raise asyncio.CancelledError


class SpawnedWorker(Worker):
  def __init__(
    self,
    runtime: Runtime,
    listener: WorkerListener,
    quest: str,
    launch: LaunchSpec,
    *,
    talk: Talk,
    timeout: Optional[float],
    launch_timeout: Optional[float] = LAUNCH_TIMEOUT,
    schedule: Scheduler = call_later,
  ):
    super().__init__(
      runtime, listener, quest, timeout=timeout, launch_timeout=launch_timeout, schedule=schedule
    )
    self._launch = launch
    self._talk = talk
    self._handle: Optional[ChildHandle] = None
    self._connected = False
    self._disconnected = asyncio.Event()

  async def _run(self) -> None:
    try:
      provisioned = await self.runtime.provision(self)
      self._bind(provisioned.channel)
      self._handle = await _owned_launch(
        self.runtime.launch(self._launch, provisioned, self.quest, self._talk)
      )
    except asyncio.CancelledError:
      raise
    except Exception as error:
      self._finish(DeathReport(self._reason_or('launch'), error=str(error)))
      return
    self._mark_started()
    code = await self._handle.wait()
    if self._connected:
      try:
        await asyncio.wait_for(self._disconnected.wait(), _DRAIN_TIMEOUT)
      except TimeoutError:
        log.warning(
          'broker worker %s channel did not disconnect within %.0fs after process exit; '
          'reporting exit without a complete drain',
          self.quest,
          _DRAIN_TIMEOUT,
        )
    self._finish(
      DeathReport(
        self._reason_or('exit'),
        exit_code=code,
        output_tail=self._handle.output_tail(),
      )
    )

  async def _kill(self) -> None:
    if self._handle is not None:
      await self._handle.kill()

  def on_connect(self) -> None:
    self._connected = True
    self._disconnected.clear()

  def on_disconnect(self) -> None:
    self._disconnected.set()


class ExpectedWorker(Worker):
  def __init__(
    self,
    runtime: Runtime,
    listener: WorkerListener,
    quest: str,
    ready,
  ):
    super().__init__(runtime, listener, quest, timeout=None, launch_timeout=None)
    self._ready = ready
    self._gone = asyncio.Event()

  async def _run(self) -> None:
    try:
      provisioned = await self.runtime.provision(self)
      self._bind(provisioned.channel)
      _, cancelled = await _own(asyncio.to_thread(self._ready, provisioned))
      if cancelled:
        raise asyncio.CancelledError
      self.listener.on_worker_ready(self)
    except asyncio.CancelledError:
      raise
    except Exception as error:
      self._finish(DeathReport(self._reason_or('launch'), error=str(error)))
      return
    await self._gone.wait()
    self._finish(DeathReport(self._reason_or('disconnected')))

  async def _kill(self) -> None:
    self._gone.set()

  def on_connect(self) -> None:
    self._mark_started()

  def on_disconnect(self) -> None:
    self._gone.set()


class JobWorker(Worker):
  def __init__(
    self,
    runtime: Runtime,
    listener: WorkerListener,
    quest: str,
    command: CommandJob,
    output: JobOutput,
    context: Any,
    requester: Peer,
    *,
    timeout: Optional[float],
    launch_timeout: Optional[float] = LAUNCH_TIMEOUT,
    schedule: Scheduler = call_later,
  ):
    super().__init__(
      runtime, listener, quest, timeout=timeout, launch_timeout=launch_timeout, schedule=schedule
    )
    self._command = command
    self._output = output
    self._context = context
    self._requester = requester
    self._directory: Optional[Path] = None
    self._handle: Optional[ChildHandle] = None
    self._reaped = False
    self._exit_code: Optional[int] = None
    self._cancel_after_kill = True

  @property
  def directory(self) -> Path:
    if self._directory is None:
      raise RuntimeError('job output directory is not open')
    return self._directory

  async def _run(self) -> None:
    self._bind(job_peer(self.quest))
    try:
      directory = self._output.open()
      self._directory = directory
    except Exception as error:
      self._finish(DeathReport('output', error=str(error)))
      return
    try:
      self._handle = await _owned_launch(self.runtime.launch_job(self._command, directory))
    except asyncio.CancelledError:
      await asyncio.to_thread(_remove_run, directory)
      raise
    except Exception as error:
      await asyncio.to_thread(_remove_run, directory)
      self._finish(DeathReport(self._reason_or('launch'), error=str(error)))
      return
    self._mark_started()
    code = await self._handle.wait()
    self._reaped = True
    self._exit_code = code
    status: dict[str, Any] = {'reason': self._reason_or('exit'), 'exit_code': code}
    clean = code == 0 and self._end_reason is None
    try:
      await asyncio.to_thread(record_status, directory, status)
      value = await self._output.collect(directory, self._context, self._requester)
      await asyncio.to_thread(_remove_run, directory)
      message = (
        brotocol.result(self.quest, 'ok', value=value)
        if clean
        else brotocol.result(self.quest, 'failed', detail={**status, **value})
      )
    except Exception as error:
      log.warning(
        'broker worker: job %s collection failed: %r; its run is kept at %s',
        self.quest,
        error,
        directory,
      )
      detail = {'reason': 'output'} if clean else status
      message = brotocol.result(self.quest, 'failed', error=str(error), detail=detail)
    self.listener.on_worker_message(self, message, host_worker=True)
    self._finish(DeathReport(status['reason'], exit_code=code))

  def _end_now(self) -> None:
    if not self._reaped:
      super()._end_now()
      return
    assert self._end_reason is not None
    if self._task is not None:
      self._task.cancel()
    self._finish(DeathReport(self._end_reason, exit_code=self._exit_code))

  async def _kill(self) -> None:
    if self._handle is not None:
      await self._handle.kill()


def _remove_run(directory: Path) -> None:
  try:
    shutil.rmtree(directory)
  except OSError as error:
    log.warning('broker worker: could not remove the job run directory %s: %s', directory, error)
