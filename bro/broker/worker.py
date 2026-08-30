"""Worker-owned supervision for spawned, job, and expected broker quests."""

import asyncio
import shutil
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from bro.base import log
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.job import CommandJob, record_status
from bro.broker.runtime import Peer, Runtime
from bro.broker.spawn import ChildHandle, LaunchSpec

LAUNCH_TIMEOUT = 1800.0
_DRAIN_TIMEOUT = 2.0


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
  ):
    self.runtime = runtime
    self.listener = listener
    self.quest = quest
    self.timeout = timeout
    self.peer: Optional[Peer] = None
    self._launch_timeout = launch_timeout
    self._task: Optional[asyncio.Task[None]] = None
    self._timer: Optional[asyncio.TimerHandle] = None
    self._started = False
    self._timed_out = False
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
    """Disarm the quest deadline after its result without ending supervision."""
    self._cancel_timer()

  def _arm(self, seconds: Optional[float]) -> None:
    self._cancel_timer()
    if seconds is not None:
      self._timer = asyncio.get_running_loop().call_later(seconds, self._deadline)

  def _deadline(self) -> None:
    self._timer = None
    if self._finished or self._stopping:
      return
    self._timed_out = True
    if not self._started:
      if self._task is not None:
        self._task.cancel()
      self._finish(DeathReport('timeout'))
      return
    asyncio.create_task(self._kill())

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
    if task.cancelled() or self._stopping:
      return
    error = task.exception()
    if error is not None and not self._finished:
      log.warning('broker worker %s failed: %r', self.quest, error)
      self._finish(DeathReport('launch' if not self._started else 'exit', error=str(error)))

  def on_connect(self) -> None:
    pass

  def on_message(self, message: Message) -> None:
    if message.type != Tag.REQUEST and message.quest_id != self.quest:
      log.warning(
        'broker worker %s refused %r for quest %s',
        self.quest,
        message.type,
        message.quest_id,
      )
      return
    if self._started:
      self.listener.on_worker_message(self, message, host_worker=False)
    else:
      self._pending_messages.append(message)

  def on_disconnect(self) -> None:
    pass


async def _owned_launch(launch: Coroutine[Any, Any, ChildHandle]) -> ChildHandle:
  task = asyncio.create_task(launch)
  cancelled = False
  while True:
    try:
      handle = await asyncio.shield(task)
      break
    except asyncio.CancelledError:
      cancelled = True
      if task.cancelled():
        raise
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
    timeout: Optional[float],
    launch_timeout: Optional[float] = LAUNCH_TIMEOUT,
  ):
    super().__init__(runtime, listener, quest, timeout=timeout, launch_timeout=launch_timeout)
    self._launch = launch
    self._handle: Optional[ChildHandle] = None
    self._connected = False
    self._disconnected = asyncio.Event()

  async def _run(self) -> None:
    try:
      provisioned = await self.runtime.provision(self)
      self._bind(provisioned.channel)
      self._handle = await _owned_launch(self.runtime.launch(self._launch, provisioned, self.quest))
    except asyncio.CancelledError:
      raise
    except Exception as error:
      self._finish(DeathReport('launch', error=str(error)))
      return
    self._mark_started()
    code = await self._handle.wait()
    if self._connected:
      try:
        await asyncio.wait_for(self._disconnected.wait(), _DRAIN_TIMEOUT)
      except TimeoutError:
        pass
    reason = 'timeout' if self._timed_out else 'exit'
    self._finish(
      DeathReport(
        reason,
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
      self._ready(provisioned)
      self.listener.on_worker_ready(self)
    except asyncio.CancelledError:
      raise
    except Exception as error:
      self._finish(DeathReport('launch', error=str(error)))
      return
    await self._gone.wait()
    self._finish(DeathReport('disconnected'))

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
  ):
    super().__init__(runtime, listener, quest, timeout=timeout, launch_timeout=launch_timeout)
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
      self._finish(DeathReport('launch', error=str(error)))
      return
    self._mark_started()
    code = await self._handle.wait()
    self._reaped = True
    self._exit_code = code
    status: dict[str, Any] = {
      'reason': 'timeout' if self._timed_out else 'exit',
      'exit_code': code,
    }
    clean = code == 0 and not self._timed_out
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

  def _deadline(self) -> None:
    if not self._reaped:
      super()._deadline()
      return
    self._timer = None
    if self._finished or self._stopping:
      return
    self._timed_out = True
    if self._task is not None:
      self._task.cancel()
    self._finish(DeathReport('timeout', exit_code=self._exit_code))

  async def _kill(self) -> None:
    if self._handle is not None:
      await self._handle.kill()


def _remove_run(directory: Path) -> None:
  try:
    shutil.rmtree(directory)
  except OSError as error:
    log.warning('broker worker: could not remove the job run directory %s: %s', directory, error)
