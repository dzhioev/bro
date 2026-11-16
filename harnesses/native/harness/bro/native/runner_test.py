import asyncio
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar, Optional

import pytest

import bro.mcp as mcp
from bro import quest, results, summon, turn_end, watches
from bro.authority_view_test_helper import publish
from bro.base import credentials
from bro.bro import AnswerDelivered, BaseBro, BroRaised
from bro.broker.brotocol import Message, encode_talk
from bro.broker.client import Client
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION, BROKER_TALK, BROKER_UPSTREAM
from bro.broker.transport import ClientTransport
from bro.broker.transports.tcp import LOCAL_HOST
from bro.inbox import Inbox
from bro.launch.broxy import session_broxy
from bro.llm.llm import Recipe
from bro.llm.mcp import InProcessMCPServer, MCPServer
from bro.llm.observer import (
  InterimAssistantTextEvent,
  NullObserver,
  ObservedEvent,
  Observer,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.llm.records import Call, DecodedCall, Message as Reply
from bro.llm.tracker import NullTracker, Tracker
from bro.llm.wire import Leg, SystemPrompt, Text, ToolOutput, UserMessage
from bro.mcp import MCPServerSpec
from bro.monitor import SESSION_DIR_ENV, trail_pointer
from bro.native.echo import Echo
from bro.native.echo_declaration import Recipe as EchoSpec
from bro.native.llm import LLM
from bro.native.runner import Runner
from bro.openai.declaration import Recipe as OpenAISpec
from bro.quest_test_helper import TIMEOUT, running_live_broker
from bro.run_lifecycle import MAX_ANSWER_BYTES, RunLifecycle


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(Inbox(), mcp_servers)
    self.response = response
    self.send_calls: list[Sequence[SystemPrompt | UserMessage]] = []
    self.wake_calls = 0

  async def send(
    self, inputs: Sequence[SystemPrompt | UserMessage], *, request_timeout: Optional[float] = None
  ) -> str:
    self.send_calls.append(inputs)
    return self.response

  async def wake(self, *, request_timeout: Optional[float] = None) -> str:
    self.inbox.drain()
    self.wake_calls += 1
    return self.response


class CapturingObserver(Observer):
  def __init__(self):
    self.events: list[ObservedEvent] = []

  def on_event(self, event: ObservedEvent) -> None:
    self.events.append(event)


class Declared(BaseBro):
  """the declaration the runner tests drive — each instance names itself, so a
  test that asserts on the recorded bro name or prompt needs no subclass."""

  description = 'd'

  def __init__(self, name: str = 'runner-bro', system_prompt: str = ''):
    self.name = name
    super().__init__(system_prompt=system_prompt)
    self.recipe = EchoSpec(model='echo')


class StubRunner(Runner):
  """runner whose LLM the test supplies instead of building one from a recipe."""

  def __init__(
    self,
    llm: Optional[LLM] = None,
    *,
    bro: Optional[BaseBro] = None,
    recipe: Recipe | None = None,
    name: str = 'runner-bro',
    system_prompt: str = '',
  ):
    super().__init__(
      bro if bro is not None else Declared(name=name, system_prompt=system_prompt),
      recipe if recipe is not None else EchoSpec(model='echo'),
    )
    self.llm = llm if llm is not None else MockLLM()

  def _create_llm(self, *, hold: str) -> LLM:
    self.llm.inbox = self.inbox
    return self.llm


def _server_layer(server_spec: MCPServerSpec) -> mcp.ToolLayer:
  return mcp.ToolLayer(server_specs=(server_spec,))


class TestRun:
  @pytest.mark.asyncio
  async def test_run_returns_response(self):
    result = await StubRunner(MockLLM(response='hello back')).run('hello', surface='test')
    assert result == 'hello back'

  @pytest.mark.asyncio
  async def test_run_emits_turn_boundaries_with_the_exact_return_value(self):
    observer = CapturingObserver()

    result = await StubRunner(MockLLM(response='exact reply')).run(
      'hello', observer=observer, surface='test'
    )

    assert result == 'exact reply'
    assert observer.events == [TurnStartedEvent('hello'), TurnCompletedEvent('exact reply')]

  @pytest.mark.asyncio
  async def test_run_owns_a_context_managed_observer(self):
    class ContextObserver(CapturingObserver):
      def __init__(self):
        super().__init__()
        self.entered = False
        self.exited = False

      def __enter__(self):
        self.entered = True
        return self

      def __exit__(self, exception_type, exception, traceback):
        self.exited = True

    observer = ContextObserver()
    await StubRunner().run('hello', observer=observer, surface='test')
    assert observer.entered is True
    assert observer.exited is True

  @pytest.mark.asyncio
  async def test_run_emits_failure_before_propagating_it(self):
    class FailingLLM(MockLLM):
      async def send(self, inputs, *, request_timeout=None):
        raise RuntimeError('provider failed')

    observer = CapturingObserver()
    with pytest.raises(RuntimeError, match='provider failed'):
      await StubRunner(FailingLLM()).run('hello', observer=observer, surface='test')
    assert observer.events == [TurnStartedEvent('hello'), TurnFailedEvent('provider failed')]

  @pytest.mark.asyncio
  async def test_run_wires_observer_through_to_llm(self):
    captured: list[Observer] = []

    class ExplicitObserver(NullObserver):
      pass

    class WireRunner(StubRunner):
      def _create_llm(self, *, hold: str) -> LLM:
        captured.append(self._observer)
        return super()._create_llm(hold=hold)

    explicit = ExplicitObserver()
    await WireRunner().run('hi', observer=explicit, surface='test')
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_run_without_an_observer_renders_nothing(self):
    runner = StubRunner()
    await runner.run('hi', surface='test')
    assert isinstance(runner._observer, NullObserver)

  @pytest.mark.asyncio
  async def test_run_explicit_tracker_overrides_default(self):
    captured: list[Tracker] = []

    class ExplicitTracker(NullTracker):
      pass

    class WireRunner(StubRunner):
      def _create_llm(self, *, hold: str) -> LLM:
        captured.append(self._tracker)
        return super()._create_llm(hold=hold)

    explicit = ExplicitTracker()
    await WireRunner().run('input', tracker=explicit, surface='test')
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_run_calls_start_and_end(self):
    calls: list[tuple[str, dict]] = []

    class RecordingTracker(NullTracker):
      def start(
        self,
        bro,
        recipe,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
      ) -> None:
        calls.append(
          (
            'start',
            {
              'bro': bro,
              'recipe': recipe,
              'system_prompt': system_prompt,
              'interactive': interactive,
              'surface': surface,
              'forked_from': forked_from,
            },
          )
        )
        return None

      def end(self, reason, detail=None) -> None:
        calls.append(('end', {'reason': reason}))

    runner = StubRunner(MockLLM(response='ok'), name='trace-bro', system_prompt='base prompt')
    await runner.run('hello', tracker=RecordingTracker(), surface='test')
    assert [c[0] for c in calls] == ['start', 'end']
    start_kwargs = calls[0][1]
    assert start_kwargs['bro'] == 'trace-bro'
    assert start_kwargs['interactive'] is False
    assert start_kwargs['surface'] == 'test'
    assert start_kwargs['forked_from'] is None
    assert 'base prompt' in start_kwargs['system_prompt']
    assert calls[1][1]['reason'] == 'ok'

  @pytest.mark.asyncio
  async def test_run_end_reason_is_raised_on_bro_raised(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def end(self, reason, detail=None) -> None:
        calls.append(reason)

    class Boom(MockLLM):
      async def send(self, inputs, *, request_timeout=None):
        raise BroRaised('nope')

    with pytest.raises(BroRaised):
      await StubRunner(Boom()).run('x', tracker=RecordingTracker(), surface='test')
    assert calls == ['raised']

  @pytest.mark.asyncio
  async def test_run_end_reason_is_error_on_generic_exception(self):
    calls: list[tuple] = []

    class RecordingTracker(NullTracker):
      def step(self, kind, body, **extras) -> None:
        calls.append(('step', kind, body))

      def end(self, reason, detail=None) -> None:
        calls.append(('end', reason))

    class Boom(MockLLM):
      async def send(self, inputs, *, request_timeout=None):
        raise RuntimeError('kaboom')

    with pytest.raises(RuntimeError):
      await StubRunner(Boom()).run('x', tracker=RecordingTracker(), surface='test')
    # the exception is recorded as an error step before the trail closes, so
    # the trail carries the failure cause rather than a bare end reason.
    assert calls == [('step', 'error', calls[0][2]), ('end', 'error')]
    body = calls[0][2]
    assert body['message'] == 'kaboom'
    assert 'RuntimeError: kaboom' in body['traceback']
    assert 'in send' in body['traceback']

  @pytest.mark.asyncio
  async def test_run_error_step_failure_does_not_mask_the_run_error(self):
    calls: list[str] = []

    class BrokenTracker(NullTracker):
      def step(self, kind, body, **extras) -> None:
        raise ConnectionError('tracker down')

      def end(self, reason, detail=None) -> None:
        calls.append(reason)

    class Boom(MockLLM):
      async def send(self, inputs, *, request_timeout=None):
        raise RuntimeError('kaboom')

    with pytest.raises(RuntimeError, match='kaboom'):
      await StubRunner(Boom()).run('x', tracker=BrokenTracker(), surface='test')
    assert calls == ['error']

  @pytest.mark.asyncio
  async def test_run_passes_system_and_user_messages(self):
    llm = MockLLM()
    await StubRunner(llm, system_prompt='be helpful').run('test input', surface='test')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemPrompt)
    assert 'be helpful' in messages[0].text
    assert messages[1] == UserMessage((Text('test input'),))

  @pytest.mark.asyncio
  async def test_run_creates_llm_with_the_unattended_hold(self):
    captured = await _captured_holds(lambda runner: runner.run('input', surface='test'))
    assert captured == ['unattended']

  @pytest.mark.asyncio
  async def test_run_hold_override_reaches_the_llm_build(self):
    captured = await _captured_holds(
      lambda runner: runner.run('input', hold='attended', surface='test')
    )
    assert captured == ['attended']


async def _captured_holds(drive) -> list[str]:
  captured: list[str] = []

  class CaptureRunner(StubRunner):
    def _create_llm(self, *, hold: str) -> LLM:
      captured.append(hold)
      return super()._create_llm(hold=hold)

  await drive(CaptureRunner())
  return captured


class TestLifetime:
  def test_exit_closes_the_live_servers(self):
    closed: list[str] = []

    class _ClosingServer(InProcessMCPServer):
      def close(self) -> None:
        closed.append(self.namespace)

    class _Holder(Declared):
      tools: ClassVar = [
        _server_layer(MCPServerSpec(namespace='holder', build=lambda: _ClosingServer('holder', [])))
      ]

    runner = StubRunner(bro=_Holder())
    with runner:
      runner.bro._live_mcp_servers('bro')
      assert closed == []
    assert closed == ['holder']

  def test_exit_survives_a_failing_server_teardown(self):
    class _BrokenServer(InProcessMCPServer):
      def close(self) -> None:
        raise RuntimeError('teardown exploded')

    class _Holder(Declared):
      tools: ClassVar = [
        _server_layer(MCPServerSpec(namespace='broken', build=lambda: _BrokenServer('broken', [])))
      ]

    runner = StubRunner(bro=_Holder())
    with runner:
      runner.bro._live_mcp_servers('bro')
    assert runner._last_end_reason == 'ok'

  def test_exit_without_send_is_safe(self):
    runner = StubRunner()
    with runner:
      pass
    assert runner.run_log is None

  @pytest.mark.parametrize(
    'exception,reason,detail',
    [
      (BroRaised('blocked'), 'raised', 'blocked'),
      (AnswerDelivered('done'), 'ok', None),
      (RuntimeError('broken'), 'error', 'RuntimeError: broken'),
    ],
  )
  def test_exit_maps_the_lifetime_outcome(self, exception, reason, detail):
    ends: list[tuple[str, Optional[str]]] = []

    class RecordingTracker(NullTracker):
      def end(self, reason, detail=None) -> None:
        ends.append((reason, detail))

    runner = StubRunner()
    runner._tracker = RecordingTracker()
    with pytest.raises(type(exception)):
      with runner:
        raise exception
    assert ends == [(reason, detail)]

  def test_keyboard_interrupt_is_a_clean_tui_exit(self):
    ends: list[str] = []

    class RecordingTracker(NullTracker):
      def end(self, reason, detail=None) -> None:
        ends.append(reason)

    runner = StubRunner()
    runner._tracker = RecordingTracker()
    with pytest.raises(KeyboardInterrupt):
      with runner:
        raise KeyboardInterrupt
    assert ends == ['ok']

  def test_exit_closes_the_run_job_registry(self):
    runner = StubRunner()
    job = runner.registry.start('sleep 30')
    with runner:
      pass

    assert job.process.wait(timeout=10) == -9

  def test_exit_stops_in_process_watches_while_the_embedding_process_lives(self, monkeypatch):
    monkeypatch.delenv(watches.OWNER_ENV, raising=False)
    runner = StubRunner()
    identity = None
    with runner:
      watch = runner.watch_store.start('sleep 30')
      identity = watch.producer_identity()
      assert identity is not None and identity.alive()

    assert identity is not None
    assert not identity.alive()

  def test_overlapping_in_process_runners_own_independent_watch_stores(self, monkeypatch):
    monkeypatch.delenv(watches.OWNER_ENV, raising=False)
    first = StubRunner()
    second = StubRunner()
    first_identity = None
    second_identity = None
    with first:
      first_watch = first.watch_store.start('sleep 30')
      first_identity = first_watch.producer_identity()
      assert first_identity is not None
      with second:
        assert second.watch_store.directory != first.watch_store.directory
        second_watch = second.watch_store.start('sleep 30')
        second_identity = second_watch.producer_identity()
        assert second_identity is not None and second_identity.alive()
      assert first_identity.alive()

    assert first_identity is not None and second_identity is not None
    assert not first_identity.alive()
    assert not second_identity.alive()

  def test_a_run_outside_a_managed_session_keeps_results_for_its_lifetime(self, monkeypatch):
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)
    runner = StubRunner()
    directory = None
    with runner:
      store = runner.result_store
      directory = store.directory
      result_id = store.keep('a::b', {}, 'kept')
      assert store.read(result_id)[1] == 'kept'

    assert directory is not None and not directory.exists()

  def test_a_run_in_a_managed_session_keeps_results_in_the_session_store(
    self, monkeypatch, tmp_path
  ):
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    runner = StubRunner()
    result_id = None
    with runner:
      result_id = runner.result_store.keep('a::b', {}, 'kept')

    assert result_id is not None
    assert results.session_store().read(result_id)[1] == 'kept'

  @pytest.mark.asyncio
  async def test_context_ends_one_interactive_conversation(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def start(self, *args, **kwargs) -> None:
        calls.append('start')
        return None

      def end(self, reason, detail=None) -> None:
        calls.append(f'end:{reason}')

    runner = StubRunner()
    with runner:
      await runner.send('first', tracker=RecordingTracker(), surface='test')
      await runner.send('second', surface='test')
    assert calls == ['start', 'end:ok']


class TestSend:
  @pytest.mark.asyncio
  async def test_send_returns_response(self):
    assert await StubRunner(MockLLM(response='hello')).send('hi', surface='test') == 'hello'

  @pytest.mark.asyncio
  async def test_send_reuses_llm(self):
    llm_instances = []

    class TrackRunner(StubRunner):
      def _create_llm(self, *, hold: str) -> LLM:
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    runner = TrackRunner()
    await runner.send('a', surface='test')
    await runner.send('b', surface='test')
    assert len(llm_instances) == 1

  @pytest.mark.asyncio
  async def test_send_first_call_includes_system_prompt(self):
    llm = MockLLM()
    await StubRunner(llm, system_prompt='be helpful').send('hi', surface='test')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemPrompt)
    assert 'be helpful' in messages[0].text
    assert messages[1] == UserMessage((Text('hi'),))

  @pytest.mark.asyncio
  async def test_send_wires_explicit_observer(self):
    captured: list[Observer] = []

    class ExplicitObserver(NullObserver):
      pass

    class WireRunner(StubRunner):
      def _create_llm(self, *, hold: str) -> LLM:
        captured.append(self._observer)
        return super()._create_llm(hold=hold)

    explicit = ExplicitObserver()
    runner = WireRunner()
    await runner.send('hi', observer=explicit, surface='test')
    await runner.send('again', surface='test')
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_send_without_an_observer_renders_nothing(self):
    runner = StubRunner()
    await runner.send('hi', surface='test')
    assert isinstance(runner._observer, NullObserver)

  @pytest.mark.asyncio
  async def test_send_first_call_starts_interactive_run(self):
    calls: list[tuple[str, dict]] = []

    class RecordingTracker(NullTracker):
      def start(
        self,
        bro,
        recipe,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
      ) -> None:
        calls.append(
          (
            'start',
            {'interactive': interactive, 'surface': surface, 'hold': hold, 'bro': bro},
          )
        )
        return None

    runner = StubRunner(name='send-bro')
    await runner.send('first', tracker=RecordingTracker(), surface='test')
    await runner.send('second', surface='test')
    assert [c[0] for c in calls] == ['start']
    assert calls[0][1]['interactive'] is True
    assert calls[0][1]['surface'] == 'test'
    assert calls[0][1]['bro'] == 'send-bro'
    assert runner.run_log is None

  @pytest.mark.asyncio
  async def test_send_labels_run_with_callers_surface(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def start(
        self,
        bro,
        recipe,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
      ) -> None:
        calls.append(surface)
        return None

    runner = StubRunner()
    await runner.send('first', tracker=RecordingTracker(), surface='call')
    await runner.send('second', surface='ignored')
    assert calls == ['call']

  @pytest.mark.asyncio
  async def test_send_rebinds_observer_on_later_sends(self):
    # a preseeded runner (bro.fork) builds its LLM before the interactive surface
    # exists; the surface's renderer must take effect when attached later.
    class MarkerObserver(NullObserver):
      pass

    runner = StubRunner()
    await runner.send('first', surface='test')
    assert runner._llm is not None
    attached = MarkerObserver()
    await runner.send('second', observer=attached, surface='test')
    assert runner._observer is attached
    assert runner._llm.observer is attached

  @pytest.mark.asyncio
  async def test_send_subsequent_calls_only_user(self):
    llm = MockLLM()
    runner = StubRunner(llm, system_prompt='be helpful')
    await runner.send('first', surface='test')
    await runner.send('second', surface='test')
    assert len(llm.send_calls) == 2
    messages = llm.send_calls[1]
    assert len(messages) == 1
    assert messages[0] == UserMessage((Text('second'),))

  @pytest.mark.asyncio
  async def test_run_does_not_affect_send_llm(self):
    llm_instances = []

    class TrackRunner(StubRunner):
      def _create_llm(self, *, hold: str) -> LLM:
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    runner = TrackRunner()
    await runner.run('one-shot', surface='test')
    await runner.send('first', surface='test')
    await runner.send('second', surface='test')
    assert len(llm_instances) == 2

  @pytest.mark.asyncio
  async def test_send_creates_llm_with_the_guided_hold(self):
    captured = await _captured_holds(lambda runner: runner.send('input', surface='test'))
    assert captured == ['guided']

  @pytest.mark.asyncio
  async def test_wake_runs_an_idle_notification_turn(self):
    observer = CapturingObserver()
    llm = MockLLM(response='noticed')
    runner = StubRunner(llm)
    await runner.send('first', observer=observer, surface='test')
    runner.inbox.post('[notification: news]')

    assert await runner.wake() == 'noticed'

    assert llm.wake_calls == 1
    assert observer.events[-1] == TurnCompletedEvent('noticed')


class _ScriptedLLM(MockLLM):
  """an LLM whose reminder turns run a scripted action each and report what they drained."""

  def __init__(self, actions: list, *, response: str = 'first reply'):
    super().__init__(response=response)
    self.actions = list(actions)
    self.drained: list[str] = []

  async def wake(self, *, request_timeout: Optional[float] = None) -> str:
    batch = self.inbox.drain()
    assert batch is not None
    self.drained.append(batch.text)
    self.wake_calls += 1
    action = self.actions.pop(0)
    return await action(self)


def _kill_and_end(job):
  async def action(llm: MockLLM) -> str:
    await asyncio.to_thread(job.kill)
    return 'settled'

  return action


class TestNativeTurnEnd:
  @pytest.mark.asyncio
  async def test_background_work_waits_silently_until_it_falls_silent(self):
    observer = CapturingObserver()
    llm = _ScriptedLLM([])
    runner = StubRunner(llm)
    runner._silence_clock = turn_end.SilenceClock(patience=0.05)
    job = runner.registry.start('sleep 30', 'bg')
    llm.actions.append(_kill_and_end(job))

    result = await runner.run('go', observer=observer, surface='test')

    assert result == 'settled'
    [notice] = llm.drained
    assert notice.startswith('[notification: background work has been silent]')
    assert 'background work: job-1 bg `sleep 30` silent for' in notice
    assert 'Ending the turn again' not in notice
    assert observer.events == [
      TurnStartedEvent('go'),
      InterimAssistantTextEvent('first reply'),
      TurnCompletedEvent('settled'),
    ]

  @pytest.mark.asyncio
  async def test_one_shot_idles_until_watch_lines_wake_it(self):
    class WatchLLM(MockLLM):
      def __init__(self):
        super().__init__(response='first reply')
        self.runner: Runner | None = None
        self.drained: list[str] = []

      async def send(self, inputs, *, request_timeout=None):
        assert self.runner is not None
        self.runner.watch_store.start('printf watched')
        return await super().send(inputs, request_timeout=request_timeout)

      async def wake(self, *, request_timeout=None):
        batch = self.inbox.drain()
        assert batch is not None
        self.drained.append(batch.text)
        self.wake_calls += 1
        return 'watch delivered'

    llm = WatchLLM()
    runner = StubRunner(llm)
    llm.runner = runner

    result = await runner.run('go', surface='test')

    assert result == 'watch delivered'
    assert any('[printf watched] watched' in batch for batch in llm.drained)

  @pytest.mark.asyncio
  async def test_a_beat_that_stalls_after_the_run_went_idle_wakes_it_once(self):
    producer = (
      f'{sys.executable} -c "from bro import watches; watches.beat(0.05); '
      'import time; time.sleep(60)"'
    )

    class StallLLM(MockLLM):
      def __init__(self):
        super().__init__(response='first reply')
        self.runner: Runner | None = None
        self.drained: list[str] = []

      async def send(self, inputs, *, request_timeout=None):
        assert self.runner is not None
        self.runner.watch_store.start(producer)
        return await super().send(inputs, request_timeout=request_timeout)

      async def wake(self, *, request_timeout=None):
        batch = self.inbox.drain()
        assert batch is not None
        self.drained.append(batch.text)
        self.wake_calls += 1
        return 'noticed'

    llm = StallLLM()
    runner = StubRunner(llm)
    llm.runner = runner

    result = await runner.run('go', surface='test')

    assert result == 'noticed'
    [notice] = llm.drained
    assert 'is alive but has stopped beating' in notice

  def test_a_job_started_while_the_inbox_waiter_idles_arms_its_silence_notice(self):
    runner = StubRunner()
    runner._silence_clock = turn_end.SilenceClock(patience=0.05)
    waiting = threading.Event()
    idle_deadline = runner._idle_deadline

    def signalling_deadline():
      deadline = idle_deadline()
      waiting.set()
      return deadline

    runner._idle_deadline = signalling_deadline
    with ThreadPoolExecutor(1) as pool, runner:
      woken = pool.submit(runner.wait_for_news, threading.Event())
      assert waiting.wait(10)

      runner.registry.start('sleep 30', 'bg')

      assert woken.result(timeout=10) is True
    drained = runner.inbox.drain()
    assert drained is not None and 'background work has been silent' in drained.text

  def test_standing_the_watchdog_down_waits_out_an_evaluation_already_past_its_check(
    self, monkeypatch
  ):
    runner = StubRunner()
    runner._silence_clock = turn_end.SilenceClock(patience=0.05)
    evaluating = threading.Event()
    release = threading.Event()
    events: list[str] = []
    silence_notice = turn_end.silence_notice

    def held_notice(port, now):
      evaluating.set()
      assert release.wait(10)
      return silence_notice(port, now)

    monkeypatch.setattr(turn_end, 'silence_notice', held_notice)
    cancelled = threading.Event()
    post = runner.inbox.post

    def recording_post(notice: str) -> None:
      events.append('posted')
      post(notice)

    monkeypatch.setattr(runner.inbox, 'post', recording_post)

    def watchdog() -> None:
      runner.wait_for_news(cancelled)

    def stand_down() -> None:
      runner.stand_down_watchdog(cancelled)
      events.append('stood down')

    with ThreadPoolExecutor(2) as pool, runner:
      runner.registry.start('sleep 30', 'bg')
      watching = pool.submit(watchdog)
      assert evaluating.wait(10)
      standing = pool.submit(stand_down)
      assert cancelled.wait(10)

      release.set()
      watching.result(timeout=10)
      standing.result(timeout=10)

    assert events == ['posted', 'stood down']

  def test_watch_pump_takes_the_next_batch_without_waiting_for_a_drain(self):
    delivered = threading.Event()

    class Store:
      def __init__(self):
        self.batches = [
          watches.Batch((watches.BatchLine(None, text, wakes=True, arrived=0.0),))
          for text in ('first', 'second')
        ]

      def next_beat_deadline(self):
        return None

      def take(self):
        return self.batches.pop(0) if len(self.batches) > 0 else None

    class PumpRunner(StubRunner):
      def __init__(self, store):
        super().__init__()
        self.store = store

      @property
      def watch_store(self):
        return self.store

      def deliver(self, batch: str) -> None:
        super().deliver(batch)
        if batch == 'second':
          delivered.set()

    store = Store()
    runner = PumpRunner(store)
    with runner:
      assert delivered.wait(10), 'the pump waited for the model to drain'
      drained = runner.inbox.drain()
      assert drained is not None and drained.text == 'first\nsecond'

  @pytest.mark.asyncio
  async def test_a_delivered_answer_ends_the_run_without_settlement(self):
    runner = StubRunner(_StubLLM(error=AnswerDelivered('the answer')))
    job = runner.registry.start('sleep 30', 'bg')

    result = await runner.run('go', surface='test')

    assert result == 'the answer'
    assert runner.inbox.drain() is None
    assert job.process.wait(timeout=10) == -9

  @pytest.mark.asyncio
  async def test_nothing_live_ends_as_before(self):
    observer = CapturingObserver()
    llm = _ScriptedLLM([], response='done')
    runner = StubRunner(llm)

    result = await runner.run('go', observer=observer, surface='test')

    assert result == 'done'
    assert llm.wake_calls == 0
    assert observer.events == [TurnStartedEvent('go'), TurnCompletedEvent('done')]


class _GatedBro(BaseBro):
  name = 'gated'
  description = 'declares secrets for credential-gate tests'
  extra_secrets = ('alpha', 'beta')

  def __init__(self):
    super().__init__(system_prompt='gated')
    self.recipe = OpenAISpec(model='gpt-5.6-terra')


@pytest.mark.credential_gate
class TestCredentialGate:
  """the run-start credential gate: every name in `needed_secrets()` plus
  `recipe.needed_secrets()` must resolve before any machinery runs."""

  def _gated(self) -> tuple[StubRunner, MockLLM]:
    llm = MockLLM(response='ran')
    return StubRunner(llm, bro=_GatedBro(), recipe=OpenAISpec(model='gpt-5.6-terra')), llm

  @pytest.mark.asyncio
  async def test_run_refuses_listing_every_missing_name(self, monkeypatch):
    monkeypatch.setattr(credentials, 'available', lambda name: False)
    gated, llm = self._gated()
    observer = CapturingObserver()
    with pytest.raises(BroRaised, match='missing credentials: alpha, beta, openai'):
      await gated.run('hi', observer=observer, surface='test')
    assert observer.events == [
      TurnStartedEvent('hi'),
      TurnFailedEvent('gated cannot start: missing credentials: alpha, beta, openai'),
    ]
    assert len(llm.send_calls) == 0

  @pytest.mark.asyncio
  async def test_run_proceeds_when_all_resolve(self, monkeypatch):
    monkeypatch.setattr(credentials, 'available', lambda name: True)
    assert await self._gated()[0].run('hi', surface='test') == 'ran'

  @pytest.mark.asyncio
  async def test_send_reports_missing_names_in_reply(self, monkeypatch):
    available = {'beta', 'openai'}
    monkeypatch.setattr(credentials, 'available', lambda name: name in available)
    gated, llm = self._gated()
    observer = CapturingObserver()
    reply = await gated.send('hi', observer=observer, surface='test')
    assert reply == 'gated cannot start: missing credentials: alpha'
    assert observer.events == [
      TurnStartedEvent('hi'),
      TurnRefusedEvent('gated cannot start: missing credentials: alpha'),
    ]
    assert len(llm.send_calls) == 0
    # the LLM stays unbuilt, so a later send re-checks the store
    available.add('alpha')
    assert await gated.send('hi', surface='test') == 'ran'


class _StubLLM(LLM):
  def __init__(self, response: str = 'ok', error: Optional[BaseException] = None):
    super().__init__(Inbox(), mcp_servers=None)
    self._response = response
    self._error = error

  async def send(
    self, inputs: Sequence[SystemPrompt | UserMessage], *, request_timeout: Optional[float] = None
  ) -> str:
    if self._error is not None:
      raise self._error
    return self._response


class _RecordingTransport(ClientTransport):
  def __init__(self):
    self.sent: list[Message] = []
    self.closed = False

  def send(self, message: Message) -> None:
    self.sent.append(message)

  def receive(self, timeout: Optional[float]) -> Optional[Message]:
    return None

  def close(self, confirm: bool = False) -> None:
    self.closed = True


class _ChannelRunner(StubRunner):
  def __init__(self, channel: Optional[RunLifecycle], llm: LLM):
    super().__init__(llm, name='channel-bro')
    self._channel = channel

  def _make_channel(self) -> Optional[RunLifecycle]:
    return self._channel


def _make_channel() -> tuple[RunLifecycle, _RecordingTransport]:
  transport = _RecordingTransport()
  return RunLifecycle(Client(transport), 'X'), transport


class TestRunLifecycle:
  """what `run()` emits over the broker channel."""

  @pytest.mark.asyncio
  async def test_terminal_run_emits_only_completed(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(response='the result'))
    result = await runner.run('input', tracker=NullTracker(), surface='test')
    assert result == 'the result'
    assert [m.type for m in transport.sent] == ['result']
    assert transport.sent[0].payload == {'outcome': 'ok', 'value': 'the result'}
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_raised_run_emits_completed_with_reason(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(error=BroRaised('cannot fulfill')))
    with pytest.raises(BroRaised):
      await runner.run('input', surface='test')
    assert [m.type for m in transport.sent] == ['result']
    assert transport.sent[0].payload == {
      'outcome': 'failed',
      'error': 'cannot fulfill',
      'detail': {'reason': 'raised'},
    }
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_error_run_emits_completed_with_exception_string(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(error=RuntimeError('kaboom')))
    with pytest.raises(RuntimeError):
      await runner.run('input', surface='test')
    assert [m.type for m in transport.sent] == ['result']
    assert transport.sent[0].payload == {
      'outcome': 'failed',
      'error': 'RuntimeError: kaboom',
      'detail': {'reason': 'error'},
    }
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_completed_follows_tracker_end(self):
    events: list[str] = []

    class OrderTracker(NullTracker):
      def start(
        self,
        bro,
        recipe,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
      ) -> None:
        events.append('start')
        return None

      def end(self, reason, detail=None) -> None:
        events.append('end')

    class OrderTransport(_RecordingTransport):
      def send(self, message: Message) -> None:
        events.append(message.type)
        super().send(message)

    transport = OrderTransport()
    runner = _ChannelRunner(RunLifecycle(Client(transport), 'X'), _StubLLM())
    await runner.run('input', tracker=OrderTracker(), surface='test')
    assert events == ['start', 'end', 'result']

  @pytest.mark.asyncio
  async def test_nested_run_leaves_the_session_pointer_unchanged(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    trail_pointer.write(tmp_path / trail_pointer.FILENAME, 'session-trail')
    runner = _ChannelRunner(None, _StubLLM())
    await runner.run('input', surface='test')
    assert trail_pointer.read(tmp_path / trail_pointer.FILENAME) == 'session-trail'

  @pytest.mark.asyncio
  async def test_run_without_channel_emits_nothing(self, monkeypatch):
    monkeypatch.delenv(BROKER_CHANNEL, raising=False)
    assert await StubRunner(_StubLLM(response='fine')).run('input', surface='test') == 'fine'

  @pytest.mark.asyncio
  async def test_send_does_not_emit_lifecycle(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(response='chat'))
    assert await runner.send('hi', surface='test') == 'chat'
    assert transport.sent == []

  @pytest.mark.asyncio
  async def test_answer_delivered_completes_the_run_with_the_answer(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(error=AnswerDelivered('the answer')))
    result = await runner.run('input', tracker=NullTracker(), surface='test')
    assert result == 'the answer'
    assert [m.type for m in transport.sent] == ['result']
    assert transport.sent[0].payload == {'outcome': 'ok', 'value': 'the answer'}

  @pytest.mark.asyncio
  async def test_summoned_send_emits_no_trail_mark(self, monkeypatch):
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    monkeypatch.setenv(BROKER_TALK, 'worker.say')
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(response='chat'))
    assert await runner.send('hi', tracker=NullTracker(), surface='test') == 'chat'
    assert transport.sent == []
    assert transport.closed is False
    assert await runner.send('more', surface='test') == 'chat'
    assert transport.sent == []

  @pytest.mark.asyncio
  async def test_send_propagates_answer_delivered_to_the_surface(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(error=AnswerDelivered('done')))
    with pytest.raises(AnswerDelivered):
      await runner.send('hi', surface='test')
    assert transport.sent == []  # the surface owns the delivery


class TestAgentIdentity:
  def test_create_llm_threads_the_agent_identity(self):
    from bro.native.echo_declaration import Recipe as EchoSpec

    class PlainBro(Declared):
      def __init__(self, name):
        super().__init__(name=name)
        self.recipe = EchoSpec(model='echo')

    assert (
      Runner(PlainBro(name='plain'), EchoSpec(model='echo'))._create_llm(hold='unattended').agent
      == 'bro//plain'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_answer', [False, True])
async def test_summoner_receives_bounded_results_from_the_run(
  monkeypatch, tmp_path, explicit_answer
):
  oversized = 'é' * (MAX_ANSWER_BYTES // 2) + 'x'
  shorter = 'condensed findings: ' + 'é' * (MAX_ANSWER_BYTES // 4)
  exchanges = 0

  async def exchange(self, inputs, tools, request_timeout):
    nonlocal exchanges
    exchanges += 1
    trail_pointer.publish('session-trail')
    if not explicit_answer:
      part = Reply(0, oversized)
    else:
      assert any(tool.name == 'bro__answer' for tool in tools)
      if exchanges == 1:
        part = Call.parse(0, 'large', 'bro__answer', {'answer': oversized})
      else:
        assert exchanges == 2
        [refusal] = inputs
        assert isinstance(refusal, ToolOutput)
        assert refusal.call_id == 'large'
        assert 'shorten it to fit' in str(refusal.output)
        part = Call.parse(0, 'short', 'bro__answer', {'answer': shorter})
    decoded = DecodedCall(self.recipe.model, {}, None, (part,), 'completed')
    return Leg.from_decoded({'request': {}, 'response': {}}, {}, 'echo', decoded, None)

  publish(monkeypatch)
  monkeypatch.setattr(Echo, 'exchange', exchange)
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / 'session'))
  monkeypatch.setenv(summon.RUNTIME_ENV, '/runtime')
  async with running_live_broker() as (spawner, summoner):
    summoner_address = summoner.channel.host_endpoint.address(LOCAL_HOST)
    monkeypatch.setenv(BROKER_CHANNEL, summoner_address)
    monkeypatch.setenv(BROKER_MISSION, summoner.quest)
    child_id = await asyncio.to_thread(summon.summon_detached, 'bro', 'findings', talk=[])
    child = await asyncio.to_thread(spawner.spawned.get, True, TIMEOUT)
    assert child.quest == child_id
    monkeypatch.delenv(BROKER_CHANNEL)
    monkeypatch.setenv(BROKER_UPSTREAM, child.channel.host_endpoint.address(LOCAL_HOST))
    monkeypatch.setenv(BROKER_MISSION, child.quest)
    monkeypatch.setenv(BROKER_TALK, encode_talk(child.talk))
    monkeypatch.setenv(summon.SUMMONED_ENV, '1')

    runner = Runner(Declared(), EchoSpec(model='echo'))
    with session_broxy():
      result = await runner.run('findings', surface='test', tracker=NullTracker())
    assert result == (shorter if explicit_answer else oversized)

    monkeypatch.setenv(BROKER_CHANNEL, summoner_address)
    monkeypatch.setenv(BROKER_MISSION, summoner.quest)
    answer = await asyncio.to_thread(quest.check, child_id, wait=True, timeout=TIMEOUT)
    child.handle.finish(0)
    assert answer.state == 'completed'
    if explicit_answer:
      assert exchanges == 2
      assert answer.answer == shorter
    else:
      assert answer.answer is not None
      marker = '\n\n[answer truncated; full response in trail session-trail]'
      assert answer.answer.endswith(marker)
      assert oversized.startswith(answer.answer.removesuffix(marker))
      assert len(answer.answer.encode()) <= MAX_ANSWER_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False, True])
async def test_runs_write_only_the_run_log_at_the_selected_location(
  monkeypatch, tmp_path, explicit
):
  from bro.native.run_log import STEP_KINDS, Reader
  from bro.workspace.paths import runtime_base

  monkeypatch.setattr('bro.trails.store.default_store', lambda: pytest.fail('store consulted'))
  monkeypatch.setattr('bro.summon.summoned_by_from_env', lambda: pytest.fail('attribution read'))
  monkeypatch.setenv('TRAILS_DISABLED', '')
  path = tmp_path / 'chosen.jsonl' if explicit else None
  runner = Runner(Declared(), EchoSpec(model='echo'), run_log=path)
  assert runner.run_log is None
  assert await runner.run('record me', surface='test') == 'record me'
  assert runner.run_log is not None
  assert (
    runner.run_log == path if explicit else runner.run_log.parent == runtime_base() / 'bro' / 'runs'
  )
  entries = Reader(runner.run_log).read(max_entries=100, max_bytes=100000)
  assert (entries[-1].value['kind'], entries[-1].value['reason']) == ('end', 'ok')
  assert [entry.value['kind'] for entry in entries if entry.value['kind'] in STEP_KINDS] == [
    'system_prompt',
    'user_input',
    'llm_call',
  ]
  assert not (runtime_base() / 'trails').exists()
  first = runner.run_log
  if explicit:
    with pytest.raises(FileExistsError):
      await runner.run('another', surface='test')
    assert runner.run_log is None
  else:
    await runner.run('another', surface='test')
    assert runner.run_log != first


@pytest.mark.asyncio
async def test_error_end_includes_the_exception_type_even_for_an_empty_message(tmp_path):
  from bro.native.run_log import Reader

  class Failed(StubRunner):
    def _create_llm(self, *, hold):
      class Failure(MockLLM):
        async def send(self, inputs, *, request_timeout=None):
          raise ValueError()

      return Failure()

  runner = Failed()
  with pytest.raises(ValueError):
    await runner.run('fail', surface='test')
  assert runner.run_log is not None
  end = Reader(runner.run_log).read(max_entries=100, max_bytes=100000)[-1].value
  assert end['reason'] == 'error' and end['detail'] == 'ValueError: '
