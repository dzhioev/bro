"""the bro-native engine: runs a bro declaration as an in-process LLM loop."""

import os
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from types import TracebackType
from typing import Any, Optional, Self

from bro.base import log
from bro.bro import AnswerDelivered, BaseBro, BroRaised
from bro.channel import BroChannel
from bro.llm.observer import (
  NullObserver,
  Observer,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.llm.tracker import EndReason, NullTracker, ToolStepSource, Tracker
from bro.native import providers as native_providers
from bro.native.llm import LLM
from bro.summon import summoned, summoned_by_from_env
from bro.trails.record.bro import Recorder

_TRAILS_DISABLED_ENV = 'TRAILS_DISABLED'


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

  def __init__(self, bro: BaseBro):
    self.bro = bro
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

  def _start_refusal(self) -> Optional[str]:
    # the run-start credential gate: the refusal listing every missing secret,
    # or None to start. checked before any machinery (tracker, LLM, live
    # servers) so a missing secret surfaces at start, not mid-run at first use;
    # each surface delivers it per its mode — run() raises and send() returns it.
    missing = self.bro.missing_secrets()
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
    self._observer = observer
    self._tracker = tracker if tracker is not None else self._make_tracker()
    llm = self._create_llm(hold=hold)
    system_prompt = self.bro.system_prompt_for(hold=hold)
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
    messages = [
      {'role': 'system', 'content': system_prompt},
      {'role': 'user', 'content': input},
    ]
    return llm, messages, trail_id

  def __enter__(self) -> Self:
    if self._lifetime_active:
      raise RuntimeError('run lifetime is already active')
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
      if channel is not None:
        channel.started(trail_id)
      result: Optional[str] = None
      try:
        with self:
          try:
            result = await llm.send(messages, request_timeout=request_timeout)
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
          channel.started(trail_id)
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

  def _make_channel(self) -> Optional[BroChannel]:
    # None (no BROKER_CHANNEL in the environment) keeps the lifecycle emission inert
    return BroChannel.from_env()

  def _create_llm(self, *, hold: str) -> LLM:
    return native_providers.create(
      self.bro.llm_spec,
      mcp_servers=self.bro.assemble(
        harness='bro', wire='bare', include_raise=hold == 'unattended', live_run=self
      ),
      observer=self._observer,
      tracker=self._tracker,
      # the LLM publishes cumulative usage under the bro's surface identity (the
      # usage file must be self-describing — an in-process run's RIDE_BRO is the
      # launcher's, not this bro's).
      agent=self.bro.agent,
    )
