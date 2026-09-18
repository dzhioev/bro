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
)
from bro.broker.job import CommandJob
from bro.broker.journal import MAX_WAIT_SECONDS, Journal, Record, Subscriber, listing_position
from bro.broker.runtime import Peer, Runtime
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.transport import Provisioned, ServerTransport
from bro.broker.worker import (
  DeathReport,
  ExpectedWorker,
  JobOutput,
  JobWorker,
  SpawnedWorker,
  Worker,
)

DEFAULT_TIMEOUT = 600.0
PING = 'ping'
QUERY = 'query'
EVENTS = 'events'
CANCEL = 'cancel'

RequestHandler = Callable[['Dispatcher', Peer, Message], None]


class Dispatcher:
  """Route requests and worker messages over journal-owned quest records."""

  def __init__(
    self,
    *,
    default_timeout: float = DEFAULT_TIMEOUT,
    job_output: Optional[JobOutput] = None,
    journal: Optional[Journal] = None,
  ):
    self._runtime: Optional[Runtime] = None
    self._default_timeout = default_timeout
    self._job_output = job_output
    self.journal = journal if journal is not None else Journal()
    self.live: dict[str, Record] = {}
    self.workers: dict[Peer, str] = {}
    self._worker_objects: set[Worker] = set()
    self._handlers: dict[str, RequestHandler] = {}
    self._active: Optional[Message] = None
    self._root: Optional[Peer] = None
    self._root_worker: Optional[Worker] = None
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
    self.deliver(peer, brotocol.Message(type=Tag.RESULT, payload=payload, quest=self._request_id()))

  def deny(self, peer: Peer, error: str) -> None:
    """Refuse worker-backed work and record the denial through the journal."""
    message = self._active_message()
    parent = self.workers.get(peer)
    self.journal.deny(message.quest_id, message.kind, parent, peer, message.args, error)
    log.warning('broker dispatcher: denied request %s: %s', message.quest_id, error)
    self.deliver(peer, brotocol.result(message.quest_id, 'denied', error=error))

  def spawn(
    self,
    launch: LaunchSpec,
    requester: Peer,
    *,
    talk: Talk,
    timeout: Optional[float] = None,
  ) -> None:
    record = self._open(requester, talk=talk)
    worker = SpawnedWorker(
      self.runtime,
      self,
      record.quest_id,
      launch,
      talk=talk,
      timeout=timeout if timeout is not None else self._default_timeout,
    )
    self._start_worker(worker)
    self._deliver_record(record, brotocol.mark(record.quest_id, 'accepted'))

  def job(self, command: CommandJob, requester: Peer, *, timeout: Optional[float] = None) -> None:
    record = self._open(requester, talk=EMPTY_TALK)
    worker = JobWorker(
      self.runtime,
      self,
      record.quest_id,
      command,
      self.job_output,
      self,
      requester,
      timeout=timeout if timeout is not None else self._default_timeout,
    )
    self._start_worker(worker)
    self._deliver_record(record, brotocol.mark(record.quest_id, 'accepted'))

  def expect(
    self,
    requester: Peer,
    *,
    talk: Talk,
    timeout: Optional[float],
    ready: Callable[[Provisioned], None],
  ) -> None:
    if timeout is not None:
      raise ValueError('expected workers have no deadline')
    record = self._open(requester, talk=talk)
    worker = ExpectedWorker(self.runtime, self, record.quest_id, ready)
    self._start_worker(worker)

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
    quest_id = self.workers.get(peer)
    worker_quest = message.quest_id
    if quest_id is None or worker_quest != quest_id:
      self._refuse(peer, message, 'no matching worker quest')
      return
    record = self.live.get(worker_quest)
    if record is None:
      self._refuse(peer, message, 'no live quest')
      return
    self._on_worker_answer(record, peer, message, host_worker=False)

  def on_worker_bound(self, worker: Worker, peer: Peer) -> None:
    record = self.live.get(worker.quest)
    if record is None:
      return
    self.journal.bind(record, peer)
    self.workers[peer] = worker.quest
    if worker is self._root_worker:
      self._root = peer

  def on_worker_ready(self, worker: Worker) -> None:
    record = self.live.get(worker.quest)
    if record is not None:
      self._deliver_record(record, brotocol.mark(record.quest_id, 'accepted'))

  def on_worker_message(self, worker: Worker, message: Message, *, host_worker: bool) -> None:
    peer = worker.peer
    if peer is None:
      raise RuntimeError(f'worker for quest {worker.quest} emitted before binding')
    if message.type == Tag.REQUEST and not host_worker:
      self._on_request(peer, message)
      return
    if message.type == Tag.MESSAGE and not host_worker:
      self._on_chat_message(peer, message)
      return
    record = self.live.get(worker.quest)
    if record is None:
      return
    self._on_worker_answer(record, peer, message, host_worker=host_worker)

  def on_worker_death(self, worker: Worker, report: DeathReport) -> None:
    record = self.live.get(worker.quest)
    if record is not None:
      detail: dict[str, Any] = {'reason': report.reason}
      if report.exit_code is not None:
        detail['exit_code'] = report.exit_code
      if report.output_tail is not None:
        detail['output_tail'] = report.output_tail
      message = brotocol.result(worker.quest, 'failed', error=report.error, detail=detail)
      self._end(record, message)
    if worker.peer is not None:
      self.workers.pop(worker.peer, None)
      if worker is not self._root_worker:
        self._orphan(worker.peer)
    self._retire(worker)
    if worker is self._root_worker and self._root_exit is not None and not self._root_exit.done():
      self._root_exit.set_result(report.exit_code if report.exit_code is not None else 1)

  async def run(self, root: LaunchSpec, *, end_on_sigterm: bool = False) -> int:
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
      quest_id = lulid()
      record = self.journal.open(quest_id, 'root', None, None, {}, talk=EMPTY_TALK)
      self.live[quest_id] = record
      root_worker = SpawnedWorker(
        self.runtime,
        self,
        quest_id,
        root,
        talk=EMPTY_TALK,
        timeout=None,
        launch_timeout=None,
      )
      self._root_worker = root_worker
      self._start_worker(root_worker)
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
    if self.journal.knows(message.quest_id):
      self._wire_deny(peer, message.quest_id, f'request id {message.quest_id} already exists')
      return
    handler = self._handlers.get(message.kind)
    if handler is None:
      self._wire_deny(peer, message.quest_id, f'unknown kind {message.kind!r}')
      return
    self.invoke(peer, message)

  def _on_chat_message(self, peer: Peer, message: Message) -> None:
    record = self.live.get(message.quest_id)
    if record is None:
      self._refuse(peer, message, 'no live quest')
      return
    sender: Optional[End]
    if peer == record.requester:
      sender = 'requester'
    elif peer == record.worker:
      sender = 'worker'
    else:
      sender = None
    if sender is None:
      self._refuse(peer, message, 'peer is not an end of the quest')
      return
    if not brotocol.message_allowed(record.talk, sender, message):
      reason = f'{sender} lacks the talk right for this message'
      self._refuse(peer, message, reason)
      self.journal.refused(record, sender, message, reason)
      return
    self.journal.message(record, sender, message)
    receiver = record.worker if sender == 'requester' else record.requester
    if receiver is None:
      log.warning(
        'broker dispatcher: dropping %r on quest %s with no receiver',
        message.type,
        record.quest_id,
      )
      return
    self.deliver(receiver, message)

  def _on_worker_answer(
    self, record: Record, peer: Peer, message: Message, *, host_worker: bool
  ) -> None:
    if message.type == Tag.MARK:
      transition = message.payload['transition']
      if transition == 'trail' and not host_worker:
        trail_id = message.payload.get('trail_id')
        if (
          not isinstance(trail_id, str)
          or len(trail_id) == 0
          or not self.journal.trail(record, trail_id)
        ):
          self._refuse(peer, message, 'invalid or duplicate trail mark')
          return
      elif transition == 'started' and host_worker:
        if not self.journal.started(record):
          self._refuse(peer, message, 'duplicate started mark')
          return
      elif transition == 'listening' and not host_worker:
        if not self.journal.listening(record):
          return
      else:
        self._refuse(peer, message, 'wrong mark origin')
        return
      self._deliver_record(record, message)
      return
    if message.type == Tag.RESULT:
      worker = self._worker_for(record.quest_id)
      if not host_worker and worker is not None and worker.ending:
        self._refuse(peer, message, 'the quest is ending; its reap owns the terminal')
        return
      self._end(record, message)
      return
    self._refuse(peer, message, 'unsupported worker message')

  def _end(self, record: Record, message: Message) -> None:
    worker = self._worker_for(record.quest_id)
    if worker is not None:
      worker.settle()
    self._deliver_record(record, message)
    self.journal.end(record, message.payload)
    self.live.pop(record.quest_id, None)

  def _deliver_record(self, record: Record, message: Message) -> None:
    if record.requester is not None and record.requester in self.workers:
      self.deliver(record.requester, message)

  def _open(self, requester: Peer, *, talk: Talk) -> Record:
    message = self._active_message()
    parent = self.workers.get(requester)
    if parent is None:
      raise RuntimeError(f'cannot open quest for unattributed peer {requester}')
    record = self.journal.open(
      message.quest_id, message.kind, parent, requester, message.args, talk=talk
    )
    self.live[record.quest_id] = record
    return record

  def _start_worker(self, worker: Worker) -> None:
    self._worker_objects.add(worker)
    worker.begin()

  def _orphan(self, requester: Peer) -> None:
    for record in [entry for entry in self.live.values() if entry.requester == requester]:
      self._require_worker(record).end('orphaned')

  def _require_worker(self, record: Record) -> Worker:
    worker = self._worker_for(record.quest_id)
    if worker is None:
      raise RuntimeError(f'live quest {record.quest_id} has no worker')
    return worker

  def _retire(self, worker: Worker) -> None:
    self._worker_objects.discard(worker)
    task = asyncio.create_task(worker.stop())
    self._retirement_tasks.add(task)
    task.add_done_callback(self._retirement_tasks.discard)
    task.add_done_callback(self._report_task)

  async def _teardown(self) -> None:
    for record in list(self.live.values()):
      worker = self._worker_for(record.quest_id)
      detached = isinstance(worker, ExpectedWorker)
      outcome = 'detached' if detached else 'killed'
      payload = {'outcome': 'failed', 'detail': {'reason': outcome}}
      self.journal.end(record, payload, outcome=outcome, reason=outcome)
      self.live.pop(record.quest_id, None)
    tasks = list(self._read_tasks)
    for task in tasks:
      task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    workers = list(self._worker_objects)
    await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)
    self._worker_objects.clear()
    retirements = list(self._retirement_tasks)
    await asyncio.gather(*retirements, return_exceptions=True)
    self._retirement_tasks.clear()
    self.workers.clear()
    await self.runtime.stop()

  def _worker_for(self, quest_id: str) -> Optional[Worker]:
    return next((worker for worker in self._worker_objects if worker.quest == quest_id), None)

  def _wire_deny(self, peer: Peer, quest_id: str, error: str) -> None:
    log.warning('broker dispatcher: denied request %s: %s', quest_id, error)
    self.deliver(peer, brotocol.result(quest_id, 'denied', error=error))

  @staticmethod
  def _refuse(peer: Peer, message: Message, reason: str) -> None:
    log.warning('broker dispatcher: refused %r from peer %s (%s)', message.type, peer, reason)

  def _request_id(self) -> str:
    return self._active_message().quest_id

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
    quest_id = args.get('id')
    if quest_id is None:
      try:
        value = self._listing_value(peer, message.quest_id, args.get('cursor'))
      except ValueError as error:
        self.reply(peer, {'outcome': 'denied', 'error': str(error)})
        return
      self.reply(peer, {'outcome': 'ok', 'value': value})
      return
    view = self._query_view(peer, quest_id)
    if view is None:
      self.reply(peer, {'outcome': 'denied', 'error': f'unknown quest id {quest_id!r}'})
      return
    wait = min(float(args.get('wait', 0)), MAX_WAIT_SECONDS)
    since = args.get('since')
    record = self.journal.records.get(quest_id)
    if (
      wait > 0
      and record is not None
      and not record.terminal
      and (since is None or record.chat_seq <= since)
    ):
      self._track_read(self._wait_query(peer, message.quest_id, quest_id, wait, since))
      return
    self.deliver(peer, self._query_message(message.quest_id, view))

  def _listing_value(self, peer: Peer, request_quest: str, cursor: Optional[str]) -> dict[str, Any]:
    records = self.journal.visible_records(peer, self.workers)
    if cursor is not None:
      position = _decode_query_cursor(cursor)
      records = [record for record in records if listing_position(record) > position]
    selected: list[Record] = []
    for record in records:
      candidate_records = [*selected, record]
      candidate: dict[str, Any] = {'quests': [entry.view() for entry in candidate_records]}
      if len(candidate_records) < len(records):
        candidate['cursor'] = _encode_query_cursor(record)
      message = brotocol.result(request_quest, 'ok', value=candidate)
      if len(message.to_bytes()) > MAX_FRAME_BYTES:
        break
      selected.append(record)
    if len(selected) == 0 and len(records) > 0:
      raise RuntimeError('one journal record exceeds the query response frame')
    value: dict[str, Any] = {'quests': [record.view() for record in selected]}
    if len(selected) < len(records):
      value['cursor'] = _encode_query_cursor(selected[-1])
    return value

  async def _wait_query(
    self,
    peer: Peer,
    request_quest: str,
    target_quest: str,
    wait: float,
    since: Optional[int],
  ) -> None:
    deadline = asyncio.get_running_loop().time() + wait
    while True:
      record = self.journal.records.get(target_quest)
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
    view = self._query_view(peer, target_quest)
    if view is None:
      self.deliver(
        peer,
        brotocol.result(request_quest, 'denied', error=f'unknown quest id {target_quest!r}'),
      )
      return
    self.deliver(peer, self._query_message(request_quest, view))

  @staticmethod
  def _query_message(request_quest: str, view: dict[str, Any]) -> Message:
    message = brotocol.result(request_quest, 'ok', value={'quest': view})
    if len(message.to_bytes()) <= MAX_FRAME_BYTES:
      return message
    messages = view.get('messages')
    if not isinstance(messages, list):
      raise RuntimeError('an oversized by-id journal view carries no message tail')

    def fit_tail(candidate: dict[str, Any]) -> Optional[Message]:
      message = brotocol.result(request_quest, 'ok', value={'quest': candidate})
      if len(message.to_bytes()) <= MAX_FRAME_BYTES:
        return message
      bounded = {**candidate, 'messages_truncated': True}
      for start in range(1, len(messages) + 1):
        bounded['messages'] = messages[start:]
        message = brotocol.result(request_quest, 'ok', value={'quest': bounded})
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
      self._track_read(self._wait_events(peer, message.quest_id, after, wait))
      return
    self.deliver(peer, self._events_message(message.quest_id, head, events))

  async def _wait_events(self, peer: Peer, request_quest: str, after: int, wait: float) -> None:
    deadline = asyncio.get_running_loop().time() + wait
    while True:
      try:
        head, events = self.journal.events_after(after, peer, self.workers)
      except ValueError as gap:
        self.deliver(peer, brotocol.result(request_quest, 'denied', error=str(gap)))
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
    self.deliver(peer, self._events_message(request_quest, head, events))

  @staticmethod
  def _events_message(request_quest: str, head: int, events: list[dict[str, Any]]) -> Message:
    selected: list[dict[str, Any]] = []
    for event in events:
      message = brotocol.result(
        request_quest,
        'ok',
        value={'head': head, 'events': [*selected, event]},
      )
      if len(message.to_bytes()) > MAX_FRAME_BYTES:
        break
      selected.append(event)
    if len(selected) == 0 and len(events) > 0:
      raise RuntimeError('one journal event exceeds the events response frame')
    return brotocol.result(request_quest, 'ok', value={'head': head, 'events': selected})

  def cancel(self, peer: Peer, message: Message) -> None:
    args = message.args
    error = _validate_cancel(args)
    if error is not None:
      self.reply(peer, {'outcome': 'denied', 'error': error})
      return
    quest_id = args['id']
    record = self.live.get(quest_id)
    if record is None or record.requester != peer:
      self.reply(
        peer,
        {'outcome': 'denied', 'error': f'no live quest {quest_id!r} requested by this peer'},
      )
      return
    self._require_worker(record).end('cancelled')
    self.reply(peer, {'outcome': 'ok'})

  def _query_view(self, peer: Peer, quest_id: str) -> Optional[dict[str, Any]]:
    record = self.journal.records.get(quest_id)
    if record is not None:
      if not self.journal.visible_by_id(peer, record, self.workers):
        return None
      return record.view(include_result=True, include_messages=True)
    view = self.journal.evicted_view(quest_id)
    if view is None:
      return None
    probe = Record(quest_id, view['kind'], view['parent'], None, {}, EMPTY_TALK)
    return view if self.journal.visible(peer, probe, self.workers) else None


class Broker:
  def __init__(
    self,
    transport: ServerTransport,
    spawner: Spawner,
    *,
    default_timeout: float = DEFAULT_TIMEOUT,
    job_output: Optional[JobOutput] = None,
  ):
    self._dispatcher = Dispatcher(default_timeout=default_timeout, job_output=job_output)
    self._dispatcher.bind(Runtime(transport, spawner))
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

  def run(self, root: LaunchSpec, *, end_on_sigterm: bool = False) -> int:
    return asyncio.run(self._dispatcher.run(root, end_on_sigterm=end_on_sigterm))

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
  follows the root's exit reaches every worker the run started.

  Entered outside the runtime lifetime, so the handler stays armed through the
  teardown: a repeated signal while workers are still being ended is held
  rather than left to kill the process around them.
  """
  if not enabled:
    yield
    return

  def end() -> None:
    if root_exit.done():
      log.warning('broker root: SIGTERM again; still tearing down the workers')
      return
    log.warning('broker root: SIGTERM; ending the run and tearing down its workers')
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


def spawn_test_handler(launch: LaunchSpec) -> RequestHandler:
  def handler(context: Dispatcher, peer: Peer, _message: Message) -> None:
    context.spawn(launch, peer, talk=EMPTY_TALK)

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
  quest_id = args.get('id')
  if quest_id is not None:
    error = _identifier_error("query 'id'", quest_id)
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
  if wait is not None and quest_id is None:
    return "query 'wait' requires 'id'"
  since = args.get('since')
  if since is not None and (not isinstance(since, int) or isinstance(since, bool) or since < 0):
    return "query 'since' must be a non-negative integer"
  if since is not None and quest_id is None:
    return "query 'since' requires 'id'"
  if cursor is not None and (quest_id is not None or wait is not None or since is not None):
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
  if len(value.encode('utf-8')) > MAX_IDENTIFIER_BYTES:
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
