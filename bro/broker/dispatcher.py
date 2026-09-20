"""Journal-backed broker dispatch and the built-in read and cancel kinds."""

import asyncio
import base64
import binascii
import contextlib
import json
import signal
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any, Optional

from bro.base import log
from bro.base.lulid import lulid
from bro.broker import brotocol
from bro.broker.brotocol import (
  EMPTY_TALK,
  MAX_FRAME_BYTES,
  MAX_IDENTIFIER_BYTES,
  End,
  Message,
  Tag,
  Talk,
  encoded_text_bytes,
)
from bro.broker.job import CommandJob
from bro.broker.journal import (
  MAX_WAIT_SECONDS,
  Journal,
  Record,
  Subscriber,
  listing_position,
  oversized_message,
)
from bro.broker.runtime import Peer, Runtime
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.supervisor import (
  DeathReport,
  ExpectedSupervisor,
  JobOutput,
  JobSupervisor,
  Scheduler,
  SpawnedSupervisor,
  Supervisor,
  call_later,
)
from bro.broker.transport import Provisioned, ServerTransport

PING = 'ping'
QUERY = 'query'
EVENTS = 'events'
CANCEL = 'cancel'

RequestHandler = Callable[['Dispatcher', Peer, Message], None]


class _SupervisorEvents:
  def __init__(self, dispatcher: 'Dispatcher'):
    self._dispatcher = dispatcher

  def on_bound(self, supervisor: Supervisor, worker: Peer) -> None:
    self._dispatcher._on_bound(supervisor, worker)

  def on_ready(self, supervisor: Supervisor) -> None:
    self._dispatcher._on_ready(supervisor)

  def on_message(self, supervisor: Supervisor, message: Message, *, from_supervisor: bool) -> None:
    self._dispatcher._on_supervisor_message(supervisor, message, from_supervisor=from_supervisor)

  def on_death(self, supervisor: Supervisor, report: DeathReport) -> None:
    self._dispatcher._on_death(supervisor, report)


class Dispatcher:
  """Route requests and worker messages over journal-owned mission records."""

  def __init__(
    self,
    *,
    job_output: Optional[JobOutput] = None,
    journal: Optional[Journal] = None,
    schedule: Scheduler = call_later,
  ):
    self._runtime: Optional[Runtime] = None
    self._job_output = job_output
    self._schedule = schedule
    self._supervisor_events = _SupervisorEvents(self)
    self.journal = journal if journal is not None else Journal()
    self.live: dict[str, Record] = {}
    self.workers: dict[Peer, str] = {}
    self._supervisors: set[Supervisor] = set()
    self._handlers: dict[str, RequestHandler] = {}
    self._active: Optional[Message] = None
    self._root: Optional[Peer] = None
    self._root_supervisor: Optional[Supervisor] = None
    self._root_exit: Optional[asyncio.Future[int]] = None
    self._read_tasks: set[asyncio.Task[None]] = set()
    self._retirement_tasks: set[asyncio.Task[None]] = set()

  def bind(self, runtime: Runtime) -> None:
    if self._runtime is not None:
      raise RuntimeError('dispatcher already bound')
    self._runtime = runtime

  @property
  def runtime(self) -> Runtime:
    if self._runtime is None:
      raise RuntimeError('dispatcher used before bind()')
    return self._runtime

  @property
  def job_output(self) -> JobOutput:
    if self._job_output is None:
      raise RuntimeError('this broker runs no jobs: it was built with no job output')
    return self._job_output

  @property
  def root(self) -> Optional[Peer]:
    return self._root

  def on(self, kind: str, handler: RequestHandler) -> None:
    if kind in self._handlers:
      raise ValueError(f'kind {kind!r} already has a handler')
    self._handlers[kind] = handler

  def deliver(self, peer: Peer, message: Message) -> None:
    delivered = brotocol.frame_safe_result(message) if message.type == Tag.RESULT else message
    self.runtime.send(peer, delivered)

  def reply(self, peer: Peer, payload: dict[str, Any]) -> None:
    self.deliver(
      peer, brotocol.Message(type=Tag.RESULT, payload=payload, request=self._request_id())
    )

  def deny(self, peer: Peer, error: str, *, type: Optional[str] = None) -> None:
    """Refuse worker-backed work and record the denial through the journal."""
    message = self._active_message()
    parent = self.workers.get(peer)
    self.journal.deny(
      message.request_id, message.kind, parent, peer, message.args, error, type=type
    )
    log.warning('broker dispatcher: denied request %s: %s', message.request_id, error)
    self.deliver(peer, brotocol.result(message.request_id, 'denied', error=error))

  def spawn(
    self,
    launch: LaunchSpec,
    spawner: Spawner,
    owner: Peer,
    *,
    type: str,
    talk: Talk,
    timeout: Optional[float],
  ) -> None:
    record = self._open(owner, type=type, talk=talk)
    supervisor = SpawnedSupervisor(
      self.runtime,
      self._supervisor_events,
      record.mission_id,
      launch,
      spawner,
      talk=talk,
      timeout=timeout,
      schedule=self._schedule,
    )
    self._start_supervisor(supervisor)
    self._deliver_record(record, brotocol.mark(record.mission_id, 'accepted'))

  def job(self, command: CommandJob, owner: Peer, *, type: str, timeout: Optional[float]) -> None:
    record = self._open(owner, type=type, talk=EMPTY_TALK)
    supervisor = JobSupervisor(
      self.runtime,
      self._supervisor_events,
      record.mission_id,
      command,
      self.job_output,
      self,
      owner,
      timeout=timeout,
      schedule=self._schedule,
    )
    self._start_supervisor(supervisor)
    self._deliver_record(record, brotocol.mark(record.mission_id, 'accepted'))

  def expect(
    self,
    owner: Peer,
    *,
    type: str,
    talk: Talk,
    ready: Callable[[Provisioned], None],
  ) -> None:
    record = self._open(owner, type=type, talk=talk)
    supervisor = ExpectedSupervisor(self.runtime, self._supervisor_events, record.mission_id, ready)
    self._start_supervisor(supervisor)

  @contextlib.contextmanager
  def _as_active(self, message: Message) -> Generator[None]:
    previous = self._active
    self._active = message
    try:
      yield
    finally:
      self._active = previous

  def invoke(self, peer: Peer, message: Message) -> None:
    with self._as_active(message):
      self._handlers[message.kind](self, peer, message)

  def on_message(self, peer: Peer, message: Message) -> None:
    if message.type == Tag.REQUEST:
      self._on_request(peer, message)
      return
    if message.type == Tag.MESSAGE:
      self._on_chat_message(peer, message)
      return
    mission_id = self.workers.get(peer)
    worker_mission = message.request_id
    if mission_id is None or worker_mission != mission_id:
      self._refuse(peer, message, 'no matching worker mission')
      return
    record = self.live.get(worker_mission)
    if record is None:
      self._refuse(peer, message, 'no live mission')
      return
    self._on_worker_answer(record, peer, message, from_supervisor=False)

  def _on_bound(self, supervisor: Supervisor, worker: Peer) -> None:
    record = self.live.get(supervisor.mission)
    if record is None:
      return
    self.journal.bind(record, worker)
    self.workers[worker] = supervisor.mission
    if supervisor is self._root_supervisor:
      self._root = worker

  def _on_ready(self, supervisor: Supervisor) -> None:
    record = self.live.get(supervisor.mission)
    if record is not None:
      self._deliver_record(record, brotocol.mark(record.mission_id, 'accepted'))

  def _on_supervisor_message(
    self, supervisor: Supervisor, message: Message, *, from_supervisor: bool
  ) -> None:
    peer = supervisor.peer
    if peer is None:
      raise RuntimeError(f'supervisor for mission {supervisor.mission} emitted before binding')
    if message.type == Tag.REQUEST and not from_supervisor:
      self._on_request(peer, message)
      return
    if message.type == Tag.MESSAGE and not from_supervisor:
      self._on_chat_message(peer, message)
      return
    record = self.live.get(supervisor.mission)
    if record is None:
      return
    self._on_worker_answer(record, peer, message, from_supervisor=from_supervisor)

  def _on_death(self, supervisor: Supervisor, report: DeathReport) -> None:
    record = self.live.get(supervisor.mission)
    if record is not None:
      detail: dict[str, Any] = {'reason': report.reason}
      if report.exit_code is not None:
        detail['exit_code'] = report.exit_code
      if report.output_tail is not None:
        detail['output_tail'] = report.output_tail
      message = brotocol.result(supervisor.mission, 'failed', error=report.error, detail=detail)
      self._end(record, message)
    if supervisor.peer is not None:
      self.workers.pop(supervisor.peer, None)
      if supervisor is not self._root_supervisor:
        self._orphan(supervisor.peer)
    self._retire(supervisor)
    if (
      supervisor is self._root_supervisor
      and self._root_exit is not None
      and not self._root_exit.done()
    ):
      self._root_exit.set_result(report.exit_code if report.exit_code is not None else 1)

  async def run(
    self, root: LaunchSpec, spawner: Spawner, *, type: str, end_on_sigterm: bool = False
  ) -> int:
    """supervise `root` until it exits and answer its exit code.

    `end_on_sigterm` is for the caller that owns the process: the run then holds
    SIGTERM until its teardown is over, ending on the signal the way it ends on
    the root's exit instead of dying to it, and leaves the default disposition
    behind. Only the main thread can take it.
    """
    loop = asyncio.get_running_loop()
    self._root_exit = loop.create_future()
    async with (
      _ended_by_signal(loop, self._root_exit, enabled=end_on_sigterm),
      self._runtime_lifetime(),
    ):
      mission_id = lulid()
      record = self.journal.open(mission_id, 'root', None, None, {}, type=type, talk=EMPTY_TALK)
      self.live[mission_id] = record
      root_supervisor = SpawnedSupervisor(
        self.runtime,
        self._supervisor_events,
        mission_id,
        root,
        spawner,
        talk=EMPTY_TALK,
        timeout=None,
        launch_timeout=None,
        schedule=self._schedule,
      )
      self._root_supervisor = root_supervisor
      self._start_supervisor(root_supervisor)
      return await self._root_exit

  @contextlib.asynccontextmanager
  async def _runtime_lifetime(self) -> AsyncGenerator[None]:
    serve_task = asyncio.create_task(self.runtime.serve())
    await asyncio.sleep(0)
    try:
      yield
    finally:
      await self._teardown()
      serve_task.cancel()
      await asyncio.gather(serve_task, return_exceptions=True)

  def stop(self) -> None:
    if self._root_exit is not None and not self._root_exit.done():
      self._root_exit.set_result(0)

  def _on_request(self, peer: Peer, message: Message) -> None:
    if self.journal.knows(message.request_id):
      self._wire_deny(peer, message.request_id, f'request id {message.request_id} already exists')
      return
    handler = self._handlers.get(message.kind)
    if handler is None:
      self._wire_deny(peer, message.request_id, f'unknown kind {message.kind!r}')
      return
    self.invoke(peer, message)

  def _on_chat_message(self, peer: Peer, message: Message) -> None:
    record = self.live.get(message.request_id)
    if record is None:
      self._refuse(peer, message, 'no live mission')
      return
    sender: Optional[End]
    if peer == record.owner:
      sender = 'owner'
    elif peer == record.worker:
      sender = 'worker'
    else:
      sender = None
    if sender is None:
      self._refuse(peer, message, 'peer is not an end of the mission')
      return
    reason = oversized_message(message.payload)
    if reason is None and not brotocol.message_allowed(record.talk, sender, message):
      reason = f'{sender} lacks the talk right for this message'
    if reason is not None:
      self._refuse(peer, message, reason)
      self.journal.refused(record, sender, message, reason)
      return
    self.journal.message(record, sender, message)
    receiver = record.worker if sender == 'owner' else record.owner
    if receiver is None:
      log.warning(
        'broker dispatcher: dropping %r on mission %s with no receiver',
        message.type,
        record.mission_id,
      )
      return
    self.deliver(receiver, message)

  def _on_worker_answer(
    self, record: Record, peer: Peer, message: Message, *, from_supervisor: bool
  ) -> None:
    if message.type == Tag.MARK:
      transition = message.payload['transition']
      if transition == 'trail' and not from_supervisor:
        trail_id = message.payload.get('trail_id')
        if (
          not isinstance(trail_id, str)
          or len(trail_id) == 0
          or not self.journal.trail(record, trail_id)
        ):
          self._refuse(peer, message, 'invalid or duplicate trail mark')
          return
      elif transition == 'started' and from_supervisor:
        if not self.journal.started(record):
          self._refuse(peer, message, 'duplicate started mark')
          return
      elif transition == 'listening' and not from_supervisor:
        if not self.journal.listening(record):
          return
      else:
        self._refuse(peer, message, 'wrong mark origin')
        return
      self._deliver_record(record, message)
      return
    if message.type == Tag.RESULT:
      supervisor = self._supervisor_for(record.mission_id)
      if not from_supervisor and supervisor is not None and supervisor.ending:
        self._refuse(peer, message, 'the mission is ending; its reap owns the terminal')
        return
      self._end(record, message)
      return
    self._refuse(peer, message, 'unsupported worker message')

  def _end(self, record: Record, message: Message) -> None:
    supervisor = self._supervisor_for(record.mission_id)
    if supervisor is not None:
      supervisor.settle()
    self._deliver_record(record, message)
    self.journal.end(record, message.payload)
    self.live.pop(record.mission_id, None)

  def _deliver_record(self, record: Record, message: Message) -> None:
    if record.owner is not None and record.owner in self.workers:
      self.deliver(record.owner, message)

  def _open(self, owner: Peer, *, type: str, talk: Talk) -> Record:
    message = self._active_message()
    parent = self.workers.get(owner)
    if parent is None:
      raise RuntimeError(f'cannot open mission for unattributed peer {owner}')
    record = self.journal.open(
      message.request_id, message.kind, parent, owner, message.args, type=type, talk=talk
    )
    self.live[record.mission_id] = record
    return record

  def _start_supervisor(self, supervisor: Supervisor) -> None:
    self._supervisors.add(supervisor)
    supervisor.begin()

  def _orphan(self, owner: Peer) -> None:
    for record in [entry for entry in self.live.values() if entry.owner == owner]:
      self._require_supervisor(record).end('orphaned')

  def _require_supervisor(self, record: Record) -> Supervisor:
    supervisor = self._supervisor_for(record.mission_id)
    if supervisor is None:
      raise RuntimeError(f'live mission {record.mission_id} has no supervisor')
    return supervisor

  def _retire(self, supervisor: Supervisor) -> None:
    self._supervisors.discard(supervisor)
    task = asyncio.create_task(supervisor.stop())
    self._retirement_tasks.add(task)
    task.add_done_callback(self._retirement_tasks.discard)
    task.add_done_callback(self._report_task)

  async def _teardown(self) -> None:
    for record in list(self.live.values()):
      supervisor = self._supervisor_for(record.mission_id)
      detached = isinstance(supervisor, ExpectedSupervisor)
      outcome = 'detached' if detached else 'killed'
      payload = {'outcome': 'failed', 'detail': {'reason': outcome}}
      self.journal.end(record, payload, outcome=outcome, reason=outcome)
      self.live.pop(record.mission_id, None)
    tasks = list(self._read_tasks)
    for task in tasks:
      task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    supervisors = list(self._supervisors)
    await asyncio.gather(*(supervisor.stop() for supervisor in supervisors), return_exceptions=True)
    self._supervisors.clear()
    retirements = list(self._retirement_tasks)
    await asyncio.gather(*retirements, return_exceptions=True)
    self._retirement_tasks.clear()
    self.workers.clear()
    await self.runtime.stop()

  def _supervisor_for(self, mission_id: str) -> Optional[Supervisor]:
    return next(
      (supervisor for supervisor in self._supervisors if supervisor.mission == mission_id), None
    )

  def _wire_deny(self, peer: Peer, mission_id: str, error: str) -> None:
    log.warning('broker dispatcher: denied request %s: %s', mission_id, error)
    self.deliver(peer, brotocol.result(mission_id, 'denied', error=error))

  @staticmethod
  def _refuse(peer: Peer, message: Message, reason: str) -> None:
    log.warning('broker dispatcher: refused %r from peer %s (%s)', message.type, peer, reason)

  def _request_id(self) -> str:
    return self._active_message().request_id

  def _active_message(self) -> Message:
    if self._active is None:
      raise RuntimeError('handler primitive called outside a request handler')
    return self._active

  def _track_read(self, coroutine) -> None:
    task = asyncio.create_task(coroutine)
    self._read_tasks.add(task)
    task.add_done_callback(self._read_tasks.discard)
    task.add_done_callback(self._report_task)

  @staticmethod
  def _report_task(task: asyncio.Task) -> None:
    if task.cancelled():
      return
    error = task.exception()
    if error is not None:
      log.warning('broker dispatcher background task failed: %r', error)

  def query(self, peer: Peer, message: Message) -> None:
    args = message.args
    error = _validate_query(args)
    if error is not None:
      self.reply(peer, {'outcome': 'denied', 'error': error})
      return
    mission_id = args.get('id')
    if mission_id is None:
      try:
        value = self._listing_value(peer, message.request_id, args.get('cursor'))
      except ValueError as error:
        self.reply(peer, {'outcome': 'denied', 'error': str(error)})
        return
      self.reply(peer, {'outcome': 'ok', 'value': value})
      return
    view = self._query_view(peer, mission_id)
    if view is None:
      self.reply(peer, {'outcome': 'denied', 'error': f'unknown mission id {mission_id!r}'})
      return
    wait = min(float(args.get('wait', 0)), MAX_WAIT_SECONDS)
    since = args.get('since')
    record = self.journal.records.get(mission_id)
    if (
      wait > 0
      and record is not None
      and not record.terminal
      and (since is None or record.chat_seq <= since)
    ):
      self._track_read(self._wait_query(peer, message.request_id, mission_id, wait, since))
      return
    self.deliver(peer, self._query_message(message.request_id, view))

  def _listing_value(self, peer: Peer, request_id: str, cursor: Optional[str]) -> dict[str, Any]:
    records = self.journal.visible_records(peer, self.workers)
    if cursor is not None:
      position = _decode_query_cursor(cursor)
      records = [record for record in records if listing_position(record) > position]
    selected: list[Record] = []
    for record in records:
      candidate_records = [*selected, record]
      candidate: dict[str, Any] = {'missions': [entry.view() for entry in candidate_records]}
      if len(candidate_records) < len(records):
        candidate['cursor'] = _encode_query_cursor(record)
      message = brotocol.result(request_id, 'ok', value=candidate)
      if len(message.to_bytes()) > MAX_FRAME_BYTES:
        break
      selected.append(record)
    if len(selected) == 0 and len(records) > 0:
      raise RuntimeError('one journal record exceeds the query response frame')
    value: dict[str, Any] = {'missions': [record.view() for record in selected]}
    if len(selected) < len(records):
      value['cursor'] = _encode_query_cursor(selected[-1])
    return value

  async def _wait_query(
    self,
    peer: Peer,
    request_id: str,
    target_mission: str,
    wait: float,
    since: Optional[int],
  ) -> None:
    deadline = asyncio.get_running_loop().time() + wait
    while True:
      record = self.journal.records.get(target_mission)
      if record is None or record.terminal or (since is not None and record.chat_seq > since):
        break
      remaining = deadline - asyncio.get_running_loop().time()
      if remaining <= 0:
        break
      changed = self.journal.change_event()
      try:
        await asyncio.wait_for(changed.wait(), remaining)
      except TimeoutError:
        break
    view = self._query_view(peer, target_mission)
    if view is None:
      self.deliver(
        peer,
        brotocol.result(request_id, 'denied', error=f'unknown mission id {target_mission!r}'),
      )
      return
    self.deliver(peer, self._query_message(request_id, view))

  @staticmethod
  def _query_message(request_id: str, view: dict[str, Any]) -> Message:
    message = brotocol.result(request_id, 'ok', value={'mission': view})
    if len(message.to_bytes()) <= MAX_FRAME_BYTES:
      return message
    messages = view.get('messages')
    if not isinstance(messages, list):
      raise RuntimeError('an oversized by-id journal view carries no message tail')
    droppable = [index for index, entry in enumerate(messages) if entry.get('pending') is not True]

    def fit_tail(candidate: dict[str, Any]) -> Optional[Message]:
      message = brotocol.result(request_id, 'ok', value={'mission': candidate})
      if len(message.to_bytes()) <= MAX_FRAME_BYTES:
        return message
      bounded = {**candidate, 'messages_truncated': True}
      for count in range(1, len(droppable) + 1):
        dropped = set(droppable[:count])
        bounded['messages'] = [
          entry for index, entry in enumerate(messages) if index not in dropped
        ]
        message = brotocol.result(request_id, 'ok', value={'mission': bounded})
        if len(message.to_bytes()) <= MAX_FRAME_BYTES:
          return message
      return None

    message = fit_tail(view)
    if message is not None:
      return message
    if 'result' in view:
      result_evicted = {**view, 'result_evicted': True}
      result_evicted.pop('result')
      message = fit_tail(result_evicted)
      if message is not None:
        return message
    raise RuntimeError('one journal record exceeds the query response frame')

  def events(self, peer: Peer, message: Message) -> None:
    args = message.args
    error = _validate_events(args)
    if error is not None:
      self.reply(peer, {'outcome': 'denied', 'error': error})
      return
    if 'after' not in args and 'wait' not in args:
      self.reply(peer, {'outcome': 'ok', 'value': {'head': self.journal.head, 'events': []}})
      return
    after = int(args.get('after', self.journal.head))
    try:
      head, events = self.journal.events_after(after, peer, self.workers)
    except ValueError as gap:
      self.reply(peer, {'outcome': 'denied', 'error': str(gap)})
      return
    wait = min(float(args.get('wait', 0)), MAX_WAIT_SECONDS)
    if len(events) == 0 and wait > 0:
      self._track_read(self._wait_events(peer, message.request_id, after, wait))
      return
    self.deliver(peer, self._events_message(message.request_id, head, events))

  async def _wait_events(self, peer: Peer, request_id: str, after: int, wait: float) -> None:
    deadline = asyncio.get_running_loop().time() + wait
    while True:
      try:
        head, events = self.journal.events_after(after, peer, self.workers)
      except ValueError as gap:
        self.deliver(peer, brotocol.result(request_id, 'denied', error=str(gap)))
        return
      if len(events) > 0:
        break
      remaining = deadline - asyncio.get_running_loop().time()
      if remaining <= 0:
        break
      changed = self.journal.change_event()
      try:
        await asyncio.wait_for(changed.wait(), remaining)
      except TimeoutError:
        break
    self.deliver(peer, self._events_message(request_id, head, events))

  @staticmethod
  def _events_message(request_id: str, head: int, events: list[dict[str, Any]]) -> Message:
    selected: list[dict[str, Any]] = []
    for event in events:
      message = brotocol.result(
        request_id,
        'ok',
        value={'head': head, 'events': [*selected, event]},
      )
      if len(message.to_bytes()) > MAX_FRAME_BYTES:
        break
      selected.append(event)
    if len(selected) == 0 and len(events) > 0:
      raise RuntimeError('one journal event exceeds the events response frame')
    return brotocol.result(request_id, 'ok', value={'head': head, 'events': selected})

  def cancel(self, peer: Peer, message: Message) -> None:
    args = message.args
    error = _validate_cancel(args)
    if error is not None:
      self.reply(peer, {'outcome': 'denied', 'error': error})
      return
    mission_id = args['id']
    record = self.live.get(mission_id)
    if record is None or record.owner != peer:
      self.reply(
        peer,
        {'outcome': 'denied', 'error': f'no live mission {mission_id!r} owned by this peer'},
      )
      return
    self._require_supervisor(record).end('cancelled')
    self.reply(peer, {'outcome': 'ok'})

  def _query_view(self, peer: Peer, mission_id: str) -> Optional[dict[str, Any]]:
    record = self.journal.records.get(mission_id)
    if record is not None:
      if not self.journal.visible_by_id(peer, record, self.workers):
        return None
      return record.view(include_result=True, include_messages=True)
    view = self.journal.evicted_view(mission_id)
    if view is None:
      return None
    probe = Record(mission_id, view['kind'], None, view['parent'], None, {}, EMPTY_TALK)
    return view if self.journal.visible(peer, probe, self.workers) else None


class Broker:
  def __init__(
    self,
    transport: ServerTransport,
    *,
    job_output: Optional[JobOutput] = None,
  ):
    self._dispatcher = Dispatcher(job_output=job_output)
    self._dispatcher.bind(Runtime(transport))
    self._dispatcher.on(QUERY, query_handler)
    self._dispatcher.on(EVENTS, events_handler)
    self._dispatcher.on(CANCEL, cancel_handler)

  @property
  def journal(self) -> Journal:
    return self._dispatcher.journal

  def subscribe(self, subscriber: Subscriber) -> None:
    self.journal.subscribe(subscriber)

  def on(self, kind: str, handler: RequestHandler) -> None:
    self._dispatcher.on(kind, handler)

  def run(
    self, root: LaunchSpec, spawner: Spawner, *, type: str, end_on_sigterm: bool = False
  ) -> int:
    return asyncio.run(
      self._dispatcher.run(root, spawner, type=type, end_on_sigterm=end_on_sigterm)
    )

  def stop(self) -> None:
    self._dispatcher.stop()


# the exit code of a run ended by SIGTERM: the shell's convention for a process
# the signal killed, since the run reports it in the signal's place
TERMINATED_EXIT_CODE = 128 + signal.SIGTERM


@contextlib.asynccontextmanager
async def _ended_by_signal(
  loop: asyncio.AbstractEventLoop, root_exit: 'asyncio.Future[int]', *, enabled: bool
) -> AsyncGenerator[None]:
  """end the run on SIGTERM rather than dying to it, so the teardown that
  follows the root's exit reaches every supervisor the run started.

  Entered outside the runtime lifetime, so the handler stays armed through the
  teardown: a repeated signal while supervisors are still ending workers is held
  rather than left to kill the process around them.
  """
  if not enabled:
    yield
    return

  def end() -> None:
    if root_exit.done():
      log.warning('broker root: SIGTERM again; still tearing down supervised workers')
      return
    log.warning('broker root: SIGTERM; ending the run and tearing down supervised workers')
    root_exit.set_result(TERMINATED_EXIT_CODE)

  loop.add_signal_handler(signal.SIGTERM, end)
  try:
    yield
  finally:
    loop.remove_signal_handler(signal.SIGTERM)


def ping_handler(context: Dispatcher, peer: Peer, message: Message) -> None:
  context.reply(peer, {'outcome': 'ok', 'value': message.args})


def query_handler(context: Dispatcher, peer: Peer, message: Message) -> None:
  context.query(peer, message)


def events_handler(context: Dispatcher, peer: Peer, message: Message) -> None:
  context.events(peer, message)


def cancel_handler(context: Dispatcher, peer: Peer, message: Message) -> None:
  context.cancel(peer, message)


def spawn_test_handler(
  launch: LaunchSpec, spawner: Spawner, *, timeout: Optional[float]
) -> RequestHandler:
  def handler(context: Dispatcher, peer: Peer, _message: Message) -> None:
    context.spawn(launch, spawner, peer, type='test', talk=EMPTY_TALK, timeout=timeout)

  return handler


def _encode_query_cursor(record: Record) -> str:
  payload = json.dumps(list(listing_position(record)), separators=(',', ':')).encode()
  return base64.urlsafe_b64encode(payload).decode().rstrip('=')


def _decode_query_cursor(cursor: str) -> tuple[bool, int, str]:
  try:
    padding = '=' * (-len(cursor) % 4)
    value = json.loads(base64.urlsafe_b64decode(cursor + padding))
  except (binascii.Error, ValueError) as error:
    raise ValueError('invalid query cursor') from error
  if (
    not isinstance(value, list)
    or len(value) != 3
    or not isinstance(value[0], bool)
    or not isinstance(value[1], int)
    or isinstance(value[1], bool)
    or not isinstance(value[2], str)
  ):
    raise ValueError('invalid query cursor')
  return value[0], value[1], value[2]


def _validate_query(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - {'id', 'wait', 'since', 'cursor'})
  if len(unknown) > 0:
    return f'unknown query field(s): {", ".join(unknown)}'
  mission_id = args.get('id')
  if mission_id is not None:
    error = _identifier_error("query 'id'", mission_id)
    if error is not None:
      return error
  cursor = args.get('cursor')
  if cursor is not None and (not isinstance(cursor, str) or len(cursor) == 0):
    return "query 'cursor' must be a non-empty string"
  wait = args.get('wait')
  if wait is not None and (
    not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0
  ):
    return "query 'wait' must be a non-negative number of seconds"
  if wait is not None and mission_id is None:
    return "query 'wait' requires 'id'"
  since = args.get('since')
  if since is not None and (not isinstance(since, int) or isinstance(since, bool) or since < 0):
    return "query 'since' must be a non-negative integer"
  if since is not None and mission_id is None:
    return "query 'since' requires 'id'"
  if cursor is not None and (mission_id is not None or wait is not None or since is not None):
    return "query 'cursor' does not combine with 'id', 'wait', or 'since'"
  return None


def _validate_cancel(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - {'id'})
  if len(unknown) > 0:
    return f'unknown cancel field(s): {", ".join(unknown)}'
  return _identifier_error("cancel 'id'", args.get('id'))


def _identifier_error(field: str, value: Any) -> Optional[str]:
  if not isinstance(value, str) or len(value) == 0:
    return f'{field} must be a non-empty string'
  if encoded_text_bytes(value) > MAX_IDENTIFIER_BYTES:
    return f'{field} exceeds the protocol identifier bound'
  return None


def _validate_events(args: dict[str, Any]) -> Optional[str]:
  unknown = sorted(set(args) - {'after', 'wait'})
  if len(unknown) > 0:
    return f'unknown events field(s): {", ".join(unknown)}'
  after = args.get('after')
  if after is not None and (not isinstance(after, int) or isinstance(after, bool) or after < 0):
    return "events 'after' must be a non-negative integer"
  wait = args.get('wait')
  if wait is not None and (
    not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0
  ):
    return "events 'wait' must be a non-negative number of seconds"
  return None
