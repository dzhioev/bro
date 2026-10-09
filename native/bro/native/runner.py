"""the bro-native engine: runs a bro declaration as an in-process LLM loop."""

import os
import tempfile
import threading
import traceback
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from pathlib import Path
from types import TracebackType
from typing import Any, Optional, Self

from bro import brash_policy, turn_end, watches
from bro.base import log
from bro.base.offload import off_loop
from bro.bro import AnswerDelivered, BaseBro, BroRaised
from bro.inbox import Inbox
from bro.jobs import Registry
from bro.llm.observer import (
  InterimAssistantTextEvent,
  NullObserver,
  Observer,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.llm.tracker import EndReason, NullTracker, ToolStepSource, Tracker
from bro.monitor import session_dir, trail_pointer
from bro.native import providers as native_providers
from bro.native.harness import BRO
from bro.native.llm import LLM
from bro.run_lifecycle import RunLifecycle
from bro.summon import summoned, summoned_by_from_env
from bro.trails.record.bro import Recorder

_TRAILS_DISABLED_ENV = 'TRAILS_DISABLED'
_WATCH_POLL_SECONDS = 0.1


def _observer_scope(observer: Observer) -> AbstractContextManager[Observer]:
  if isinstance(observer, AbstractContextManager):
    return observer
  return nullcontext(observer)


def _default_factory() -> Tracker:
  # explicit kill switch wins over everything: define `TRAILS_DISABLED` (to any
  # value, presence is what counts — same convention as `NO_COLOR` /
  # `RIDE_IN_CONTAINER`) to skip recording for a process — local dev, ad-hoc runs,
  # or repairing trails-server itself (recording is otherwise mandatory and
  # crash-on-failure, so a broken server blocks every bro). this only governs the default
  # factory: a per-run `tracker=` and a custom `set_default_tracker_factory(...)`
  # still take precedence.
  if os.environ.get(_TRAILS_DISABLED_ENV) is not None:
    return NullTracker()
  # recording is otherwise on, and `NullTracker` opt-in:
  # - kill switch: `TRAILS_DISABLED` set in the environment.
  # - tests: `conftest.py`'s `set_default_tracker_factory(NullTracker)`.
  # - one-shot exploration: `Runner(bro).run(..., surface='experiment', tracker=NullTracker())`.
  from bro.trails.store import default_store

  return Recorder(default_store())


# default factory for the per-run `Tracker` an unconfigured runner uses. swap with
# `set_default_tracker_factory(...)` — `conftest.py` pins it to `NullTracker`
# so tests never try to record.
_default_tracker_factory: Callable[[], Tracker] = _default_factory


def set_default_tracker_factory(factory: Callable[[], Tracker]) -> None:
  global _default_tracker_factory
  _default_tracker_factory = factory


class Runner:
  """one bro-native conversation: the LLM it is sent through, the trail it
  records to, and the observer and broker channel it reports through.

  `run()` is the one-shot path and owns its own lifetime; an interactive owner
  keeps the runner's lifetime around the whole conversation and calls `send()`
  per turn. Satisfies `bro.bro.LiveRun`, so the service tools the assembled
  toolset mounts report against this run.
  """

  def __init__(self, bro: BaseBro, *, activity_file: Optional[Path] = None):
    self.bro = bro
    self.activity_file = activity_file
    self.inbox = Inbox()
    self.registry = Registry(self.inbox)
    self._watch_owner: Optional[watches.Owner] = None
    self._brash_policy: Optional[Path] = None
    self._lifetime_resources: Optional[ExitStack] = None
    self._watch_pump_cancelled = threading.Event()
    self._watch_delivery_lock = threading.Lock()
    self._watch_delivery_pending = False
    self._watch_pump_error: Optional[BaseException] = None
    self._turn_ended = False
    self._llm: Optional[LLM] = None
    # a bro renders only through an observer its caller passes: an embedding
    # application must not get terminal output, or a display session, it never
    # asked for.
    self._observer: Observer = NullObserver()
    # sibling of _observer — the tracker records the run for offline analysis
    # rather than rendering it to stderr. swapped in run() / send() the same way
    # _observer is.
    self._tracker: Tracker = NullTracker()
    # the id of the trail this run records to — set when the trail opens (first
    # send / run start, or by bro.fork on a preseeded runner); None until then
    # and when recording is off. surfaces read it to point the user at the
    # recorded conversation (e.g. `call`'s resume hint).
    self.trail_id: Optional[str] = None
    self._lifetime_active = False
    self._last_end_reason: Optional[EndReason] = None
    self._last_end_detail: Optional[str] = None

  @property
  def current_tool_step_id(self) -> Optional[ToolStepSource]:
    return self._tracker.current_tool_step_id

  @property
  def watch_store(self) -> watches.Store:
    if self._watch_owner is not None:
      return self._watch_owner.store
    return watches.session_store()

  @property
  def brash_policy(self) -> Optional[Path]:
    """the brash policy this run's job and watch lines start under, written
    when its lifetime starts; None where they run in bash."""
    if not self._lifetime_active:
      raise RuntimeError('a run has a brash policy only while its lifetime is active')
    return self._brash_policy

  @contextmanager
  def _watch_pump(self) -> Generator[None, None, None]:
    self._watch_pump_cancelled.clear()
    self._watch_pump_error = None
    thread = threading.Thread(target=self._pump_watch_lines, daemon=True)
    thread.start()
    try:
      yield
    finally:
      self._watch_pump_cancelled.set()
      self.inbox.notify()
      thread.join()

  def _pump_watch_lines(self) -> None:
    try:
      while not self._watch_pump_cancelled.is_set():
        with self._watch_delivery_lock:
          batch = self.watch_store.take()
          self._watch_delivery_pending = batch is not None
        if batch is not None:
          self.deliver(batch)
        else:
          self._watch_pump_cancelled.wait(_WATCH_POLL_SECONDS)
    except BaseException as error:
      self._watch_pump_error = error
      self.inbox.post('[notification: native watch delivery failed]')

  def deliver(self, batch: str) -> None:
    delivery_id = self.inbox.post(batch)
    with self._watch_delivery_lock:
      self._watch_delivery_pending = False
    self.inbox.wait_until_drained(delivery_id, self._watch_pump_cancelled)

  def background_work(self) -> tuple[str, ...]:
    result = []
    for job in (item.status() for item in self.registry.values()):
      if job.state == 'running':
        command = job.command.replace('`', '\\`')
        result.append(f'{job.id} {job.mode} `{command}`')
    return tuple(result)

  def notify(self, text: str) -> None:
    self.inbox.post(text)

  def end(self) -> None:
    self._turn_ended = True

  def _raise_watch_pump_error(self) -> None:
    if self._watch_pump_error is not None:
      raise RuntimeError('native watch delivery failed') from self._watch_pump_error

  def _settle_turn_end(self) -> bool:
    with self._watch_delivery_lock:
      self._raise_watch_pump_error()
      self._turn_ended = False
      if self._watch_delivery_pending or self.inbox.has_news():
        return False
      turn_end.settle(self)
      return self._turn_ended

  def _start_refusal(self) -> Optional[str]:
    # the run-start credential gate: the refusal listing every missing secret,
    # or None to start. checked before any machinery (tracker, LLM, live
    # servers) so a missing secret surfaces at start, not mid-run at first use;
    # each surface delivers it per its mode — run() raises and send() returns it.
    missing = self.bro.missing_secrets(BRO)
    if len(missing) == 0:
      return None
    return f'{self.bro.name} cannot start: missing credentials: {", ".join(missing)}'

  def _start(
    self,
    input: str,
    *,
    interactive: bool,
    hold: str,
    observer: Observer,
    tracker: Optional[Tracker],
    surface: str,
    summoned_by: Optional[dict[str, Any]],
  ) -> tuple[LLM, list[dict], str]:
    # the shared start sequence of run() and send(): pin the resolved observer
    # and the tracker — a caller-supplied one wins (tests inject recording
    # fakes) — on self before _create_llm, so the LLM construction path picks
    # them up, then build the LLM, compose the hold prompt, open the trail, and
    # seed the message list.
    if self.registry.closed:
      self.inbox = Inbox()
      self.registry = Registry(self.inbox)
    self._observer = observer
    self._tracker = tracker if tracker is not None else self._make_tracker()
    llm = self._create_llm(hold=hold)
    system_prompt = self.bro.system_prompt_for(hold=hold, harness=BRO)
    trail_id = self._tracker.start_trail(
      bro=self.bro.name,
      llm_spec=self.bro.llm_spec.dump(),
      system_prompt=system_prompt,
      forked_from=None,
      interactive=interactive,
      surface=surface,
      hold=hold,
      summoned_by=summoned_by,
    )
    self.trail_id = trail_id if len(trail_id) > 0 else None
    if self.trail_id is not None:
      trail_pointer.publish(self.trail_id)
    messages = [
      {'role': 'system', 'content': system_prompt},
      {'role': 'user', 'content': input},
    ]
    return llm, messages, trail_id

  def __enter__(self) -> Self:
    if self._lifetime_active:
      raise RuntimeError('run lifetime is already active')
    with ExitStack() as resources:
      resources.callback(self.registry.close)
      if session_dir() is None or os.environ.get(watches.OWNER_ENV) is None:
        self._watch_owner = resources.enter_context(
          watches.Owner.temporary(publish_environment=False)
        )
      policy_directory = resources.enter_context(tempfile.TemporaryDirectory(prefix='bro-brash-'))
      self._brash_policy = brash_policy.write(Path(policy_directory), self.bro.reach())
      resources.enter_context(self._watch_pump())
      self._lifetime_resources = resources.pop_all()
    self._lifetime_active = True
    self._last_end_reason = None
    self._last_end_detail = None
    return self

  def __exit__(
    self,
    exception_type: Optional[type[BaseException]],
    exception: Optional[BaseException],
    exception_traceback: Optional[TracebackType],
  ) -> bool:
    del exception_type, exception_traceback
    if self._lifetime_active is not True:
      raise RuntimeError('run lifetime is not active')

    reason: EndReason = 'ok'
    detail: Optional[str] = None
    if isinstance(exception, BroRaised):
      reason = 'raised'
      detail = exception.reason
    elif isinstance(exception, AnswerDelivered):
      pass  # a summoned run's clean end: the surface delivers the answer
    elif isinstance(exception, Exception):
      reason = 'error'
      detail = str(exception)
      self._record_error_step(exception)

    resources = self._lifetime_resources
    if resources is None:
      raise RuntimeError('run lifetime has no resources')
    self._lifetime_resources = None
    self._watch_owner = None
    self._brash_policy = None
    resources.close()
    self.bro.close()
    self._lifetime_active = False
    self._last_end_reason = reason
    self._last_end_detail = detail
    log.verbose('run lifetime ended: %s', reason)
    self._tracker.end_trail(reason, detail=detail)
    return False

  async def run(
    self,
    input: str,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    request_timeout: Optional[float] = None,
    *,
    surface: str,
    hold: str = 'unattended',
  ) -> str:
    effective_observer = observer if observer is not None else NullObserver()
    with _observer_scope(effective_observer):
      effective_observer.on_event(TurnStartedEvent(input))
      refusal = self._start_refusal()
      if refusal is not None:
        effective_observer.on_event(TurnFailedEvent(refusal))
        raise BroRaised(refusal)
      try:
        llm, messages, trail_id = self._start(
          input,
          interactive=False,
          hold=hold,
          observer=effective_observer,
          tracker=tracker,
          surface=surface,
          summoned_by=summoned_by_from_env(),
        )
      except Exception as error:
        effective_observer.on_event(TurnFailedEvent(str(error)))
        raise
      log.info('run started%s', f' (trail {trail_id})' if len(trail_id) > 0 else '')
      channel = self._make_channel()
      if channel is not None and len(trail_id) > 0:
        channel.trail(trail_id)
      result: Optional[str] = None
      try:
        with self:
          try:
            result = await self._turns_until_settled(llm, messages, request_timeout=request_timeout)
          except AnswerDelivered as delivered:
            # the `answer` service tool's explicit end: the answer is the result
            result = delivered.answer
          except Exception as error:
            effective_observer.on_event(TurnFailedEvent(str(error)))
            raise
          effective_observer.on_event(TurnCompletedEvent(result))
          return result
      finally:
        if channel is not None:
          if self._last_end_reason is None:
            raise RuntimeError('run lifetime ended without an outcome')
          channel_result = result if self._last_end_reason == 'ok' else self._last_end_detail
          channel.completed(channel_result, self._last_end_reason)
          channel.close()

  async def send(
    self,
    message: str,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    request_timeout: Optional[float] = None,
    *,
    surface: str,
    hold: str = 'guided',
  ) -> str:
    if self._llm is None:
      effective_observer = observer if observer is not None else NullObserver()
      effective_observer.on_event(TurnStartedEvent(message))
      refusal = self._start_refusal()
      if refusal is not None:
        # in-reply report; the LLM stays unbuilt, so a later send re-checks
        effective_observer.on_event(TurnRefusedEvent(refusal))
        return refusal
      # the tracker is locked in on first send (the LLM is constructed once and
      # records one trail); later calls can't swap it. surface (the trail
      # header's surface label) and hold are locked in the same way.
      try:
        self._llm, messages, trail_id = self._start(
          message,
          interactive=True,
          hold=hold,
          observer=effective_observer,
          tracker=tracker,
          surface=surface,
          summoned_by=summoned_by_from_env(),
        )
      except Exception as error:
        effective_observer.on_event(TurnFailedEvent(str(error)))
        raise
      if summoned():
        # a summoned interactive run announces its trail like a summoned run()
        # would — the summoner's wait re-arms on it; an un-summoned conversation
        # announces nothing (its channel is the enclosing session's, not its own)
        channel = self._make_channel()
        if channel is not None:
          if len(trail_id) > 0:
            channel.trail(trail_id)
          channel.close()
    else:
      if observer is not None:
        # unlike the tracker, the observer is rebindable mid-conversation: a
        # preseeded runner (bro.fork) built its LLM before the interactive
        # surface existed, so the surface attaches its renderer on its first send.
        self._observer = observer
        self._llm.observer = observer
      effective_observer = self._observer
      effective_observer.on_event(TurnStartedEvent(message))
      messages = [{'role': 'user', 'content': message}]
    try:
      result = await self._llm.send(messages, request_timeout=request_timeout)
    except AnswerDelivered:
      raise  # a summoned conversation's clean end — the surface delivers it
    except Exception as error:
      effective_observer.on_event(TurnFailedEvent(str(error)))
      raise
    effective_observer.on_event(TurnCompletedEvent(result))
    return result

  async def _turns_until_settled(
    self, llm: LLM, messages: list[dict], *, request_timeout: Optional[float]
  ) -> str:
    reply = await llm.send(messages, request_timeout=request_timeout)
    while not await off_loop(self._settle_turn_end):
      self._observer.on_event(InterimAssistantTextEvent(reply))
      with self.inbox.waiter() as cancelled:
        await off_loop(self.inbox.wait, None, cancelled)
      self._raise_watch_pump_error()
      reply = await llm.wake(request_timeout=request_timeout)
    return reply

  async def wake(self, request_timeout: Optional[float] = None) -> str:
    """Run one interactive turn from pending inbox news."""
    if self._llm is None:
      raise RuntimeError('cannot wake a conversation before its first turn')
    self._raise_watch_pump_error()
    observer = self._observer
    try:
      result = await self._llm.wake(request_timeout=request_timeout)
    except AnswerDelivered:
      raise
    except Exception as error:
      observer.on_event(TurnFailedEvent(str(error)))
      raise
    observer.on_event(TurnCompletedEvent(result))
    return result

  def _record_error_step(self, error: BaseException) -> None:
    # best-effort: recording the failure must never mask it — the tracker may
    # well be down for the same reason the run is failing.
    try:
      self._tracker.step(
        'error', {'message': str(error), 'traceback': ''.join(traceback.format_exception(error))}
      )
    except Exception as step_error:
      log.warning('failed to record the error step: %s', step_error)

  def _make_tracker(self) -> Tracker:
    return _default_tracker_factory()

  def _make_channel(self) -> Optional[RunLifecycle]:
    return RunLifecycle.from_env()

  def _create_llm(self, *, hold: str) -> LLM:
    return native_providers.create(
      self.bro.llm_spec,
      self.inbox,
      mcp_servers=self.bro.assemble(harness=BRO, include_raise=hold == 'unattended', live_run=self),
      observer=self._observer,
      tracker=self._tracker,
      # the LLM publishes cumulative usage under the bro's surface identity (the
      # usage file must be self-describing — an in-process run's RIDE_BRO is the
      # launcher's, not this bro's).
      agent=self.bro.agent,
      activity_file=self.activity_file,
    )
