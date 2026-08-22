import os
from typing import ClassVar, Optional

import pytest

import bro.mcp as mcp
import bro.native.runner as native_runner
from bro.base import credentials
from bro.bro import AnswerDelivered, BaseBro, BroRaised
from bro.broker.brotocol import Message
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.transport import ClientTransport
from bro.channel import BroChannel
from bro.llm.mcp import InProcessMCPServer, MCPServer
from bro.llm.observer import (
  NullObserver,
  ObservedEvent,
  Observer,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.llm.tracker import NullTracker, Tracker
from bro.mcp import MCPServerSpec
from bro.native.llm import LLM
from bro.native.runner import Runner, set_default_tracker_factory


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    self.send_calls.append(messages)
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


class StubRunner(Runner):
  """runner whose LLM the test supplies instead of building one from a recipe."""

  def __init__(
    self,
    llm: Optional[LLM] = None,
    *,
    bro: Optional[BaseBro] = None,
    name: str = 'runner-bro',
    system_prompt: str = '',
  ):
    super().__init__(bro if bro is not None else Declared(name=name, system_prompt=system_prompt))
    self.llm = llm if llm is not None else MockLLM()

  def _create_llm(self, *, hold: str) -> LLM:
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
      async def send(self, messages, *, request_timeout=None):
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
  async def test_run_calls_start_and_end_trail(self):
    calls: list[tuple[str, dict]] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self,
        bro,
        llm_spec,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
        summoned_by=None,
      ) -> str:
        calls.append(
          (
            'start',
            {
              'bro': bro,
              'llm_spec': llm_spec,
              'system_prompt': system_prompt,
              'interactive': interactive,
              'surface': surface,
              'forked_from': forked_from,
              'summoned_by': summoned_by,
            },
          )
        )
        return 'tid'

      def end_trail(self, reason, detail=None) -> None:
        calls.append(('end', {'reason': reason}))

    runner = StubRunner(MockLLM(response='ok'), name='trace-bro', system_prompt='base prompt')
    await runner.run('hello', tracker=RecordingTracker(), surface='test')
    assert [c[0] for c in calls] == ['start', 'end']
    start_kwargs = calls[0][1]
    assert start_kwargs['bro'] == 'trace-bro'
    assert start_kwargs['interactive'] is False
    assert start_kwargs['surface'] == 'test'
    assert start_kwargs['forked_from'] is None
    assert start_kwargs['summoned_by'] is None
    assert 'base prompt' in start_kwargs['system_prompt']
    assert calls[1][1]['reason'] == 'ok'

  @pytest.mark.asyncio
  async def test_run_passes_summoned_by_from_the_launch_env(self, monkeypatch):
    captured: list[Optional[dict]] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self,
        bro,
        llm_spec,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
        summoned_by=None,
      ) -> str:
        captured.append(summoned_by)
        return 'tid'

    monkeypatch.setenv('RIDE_SUMMONER', '{"trail_id":"T-parent"}')
    await StubRunner().run('hello', tracker=RecordingTracker(), surface='test')
    assert captured == [{'trail_id': 'T-parent'}]
    # consumed on read: a nested in-process run spawned by this process's tools
    # must not inherit the marker and re-stamp the parent's summoned_by
    assert 'RIDE_SUMMONER' not in os.environ
    await StubRunner().run('again', tracker=RecordingTracker(), surface='test')
    monkeypatch.setenv('RIDE_SUMMONER', '{"trail_id":"T-universal","step_id":7,"index":2}')
    await StubRunner().run('universal pointer', tracker=RecordingTracker(), surface='test')
    monkeypatch.setenv('RIDE_SUMMONER', '{"target":"bro","trail_id":"T-legacy"}')
    await StubRunner().run('legacy direct', tracker=RecordingTracker(), surface='test')
    monkeypatch.setenv('RIDE_SUMMONER', '{"session":"c:legacy-root"}')
    await StubRunner().run('legacy session', tracker=RecordingTracker(), surface='test')
    assert captured == [
      {'trail_id': 'T-parent'},
      None,
      {'trail_id': 'T-universal', 'step_id': 7, 'index': 2},
      {'trail_id': 'T-legacy'},
      None,
    ]

  @pytest.mark.asyncio
  async def test_run_end_reason_is_raised_on_bro_raised(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def end_trail(self, reason, detail=None) -> None:
        calls.append(reason)

    class Boom(MockLLM):
      async def send(self, messages, *, request_timeout=None):
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

      def end_trail(self, reason, detail=None) -> None:
        calls.append(('end', reason))

    class Boom(MockLLM):
      async def send(self, messages, *, request_timeout=None):
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

      def end_trail(self, reason, detail=None) -> None:
        calls.append(reason)

    class Boom(MockLLM):
      async def send(self, messages, *, request_timeout=None):
        raise RuntimeError('kaboom')

    with pytest.raises(RuntimeError, match='kaboom'):
      await StubRunner(Boom()).run('x', tracker=BrokenTracker(), surface='test')
    assert calls == ['error']

  def test_default_tracker_factory_can_be_swapped(self):
    sentinel = NullTracker()

    set_default_tracker_factory(lambda: sentinel)
    try:
      assert StubRunner()._make_tracker() is sentinel
    finally:
      set_default_tracker_factory(NullTracker)

  def test_default_factory_records_without_a_trails_config(self, monkeypatch, tmp_path):
    # `_default_factory` refuses to silently fall back to `NullTracker`: with no
    # credential to select a backend, the run records to local storage.
    from bro.trails.local import LocalStore
    from bro.trails.record.bro import Recorder

    monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path))
    monkeypatch.setattr(credentials, 'BRO_DIR', str(tmp_path))
    monkeypatch.setattr(credentials, '_default_store', None)
    monkeypatch.setattr('bro.trails.store.paths.project_root', lambda: tmp_path)
    monkeypatch.delenv(native_runner._TRAILS_DISABLED_ENV, raising=False)

    tracker = native_runner._default_factory()

    assert isinstance(tracker, Recorder)
    assert isinstance(tracker._store, LocalStore)

  # presence is what counts (same convention as NO_COLOR / RIDE_IN_CONTAINER):
  # any value, including '' and '0', enables the switch. unset it to record.
  @pytest.mark.parametrize('value', ['1', '', '0', 'whatever'])
  def test_default_factory_disabled_by_env_var(self, monkeypatch, value):
    # the kill switch wins before any backend resolution.
    monkeypatch.setenv(native_runner._TRAILS_DISABLED_ENV, value)
    assert isinstance(native_runner._default_factory(), NullTracker)

  @pytest.mark.asyncio
  async def test_run_passes_system_and_user_messages(self):
    llm = MockLLM()
    await StubRunner(llm, system_prompt='be helpful').run('test input', surface='test')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert 'be helpful' in messages[0]['content']
    assert messages[1] == {'role': 'user', 'content': 'test input'}

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
    # what a session holds — the dev toolset's background jobs are the built-in
    # case — is released when its lifetime ends, not at interpreter exit.
    closed: list[str] = []

    class _ClosingServer(InProcessMCPServer):
      def close(self) -> None:
        closed.append(self.namespace)

    class _Holder(Declared):
      tools: ClassVar = [_server_layer(MCPServerSpec(build=lambda: _ClosingServer('holder', [])))]

    runner = StubRunner(bro=_Holder())
    with runner:
      runner.bro._live_mcp_servers()
      assert closed == []
    assert closed == ['holder']

  def test_exit_survives_a_failing_server_teardown(self):
    class _BrokenServer(InProcessMCPServer):
      def close(self) -> None:
        raise RuntimeError('teardown exploded')

    class _Holder(Declared):
      tools: ClassVar = [_server_layer(MCPServerSpec(build=lambda: _BrokenServer('broken', [])))]

    runner = StubRunner(bro=_Holder())
    with runner:
      runner.bro._live_mcp_servers()
    assert runner._last_end_reason == 'ok'

  def test_exit_without_send_is_safe(self):
    runner = StubRunner()
    with runner:
      pass
    assert runner.trail_id is None

  @pytest.mark.parametrize(
    'exception,reason,detail',
    [
      (BroRaised('blocked'), 'raised', 'blocked'),
      (AnswerDelivered('done'), 'ok', None),
      (RuntimeError('broken'), 'error', 'broken'),
    ],
  )
  def test_exit_maps_the_lifetime_outcome(self, exception, reason, detail):
    ends: list[tuple[str, Optional[str]]] = []

    class RecordingTracker(NullTracker):
      def end_trail(self, reason, detail=None) -> None:
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
      def end_trail(self, reason, detail=None) -> None:
        ends.append(reason)

    runner = StubRunner()
    runner._tracker = RecordingTracker()
    with pytest.raises(KeyboardInterrupt):
      with runner:
        raise KeyboardInterrupt
    assert ends == ['ok']

  @pytest.mark.asyncio
  async def test_context_ends_one_interactive_conversation(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def start_trail(self, *args, **kwargs) -> str:
        calls.append('start')
        return 'tid'

      def end_trail(self, reason, detail=None) -> None:
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
    assert messages[0]['role'] == 'system'
    assert 'be helpful' in messages[0]['content']
    assert messages[1] == {'role': 'user', 'content': 'hi'}

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
  async def test_send_first_call_starts_interactive_trail(self):
    calls: list[tuple[str, dict]] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self,
        bro,
        llm_spec,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
        summoned_by=None,
      ) -> str:
        calls.append(
          (
            'start',
            {'interactive': interactive, 'surface': surface, 'hold': hold, 'bro': bro},
          )
        )
        return 'tid'

    runner = StubRunner(name='send-bro')
    await runner.send('first', tracker=RecordingTracker(), surface='test')
    await runner.send('second', surface='test')
    # start_trail fires once — interactive trails span the whole conversation.
    assert [c[0] for c in calls] == ['start']
    assert calls[0][1]['interactive'] is True
    assert calls[0][1]['surface'] == 'test'
    assert calls[0][1]['bro'] == 'send-bro'
    assert runner.trail_id == 'tid'

  @pytest.mark.asyncio
  async def test_send_labels_trail_with_callers_surface(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self,
        bro,
        llm_spec,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
        summoned_by=None,
      ) -> str:
        calls.append(surface)
        return 'tid'

    runner = StubRunner()
    await runner.send('first', tracker=RecordingTracker(), surface='call')
    # surface labels the trail header, so only the opening send matters
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
    assert messages[0] == {'role': 'user', 'content': 'second'}

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


class _GatedBro(BaseBro):
  name = 'gated'
  description = 'declares secrets for credential-gate tests'
  # two manifest names on top of the default openai spec's `openai`
  extra_secrets = ('alpha', 'beta')

  def __init__(self):
    super().__init__(system_prompt='gated')


@pytest.mark.credential_gate
class TestCredentialGate:
  """the run-start credential gate: every name in `needed_secrets()` plus
  `llm_spec.needed_secrets()` must resolve before any machinery runs."""

  def _gated(self) -> tuple[StubRunner, MockLLM]:
    llm = MockLLM(response='ran')
    return StubRunner(llm, bro=_GatedBro()), llm

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
    super().__init__(mcp_servers=None)
    self._response = response
    self._error = error

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
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
  def __init__(self, channel: Optional[BroChannel], llm: LLM):
    super().__init__(llm, name='channel-bro')
    self._channel = channel

  def _make_channel(self) -> Optional[BroChannel]:
    return self._channel


def _make_channel() -> tuple[BroChannel, _RecordingTransport]:
  transport = _RecordingTransport()
  return BroChannel(Client(transport), 'X'), transport


class _TrailIDTracker(NullTracker):
  def start_trail(
    self,
    bro,
    llm_spec,
    system_prompt,
    forked_from,
    interactive,
    surface,
    hold='unattended',
    summoned_by=None,
  ) -> str:
    return 'trail-42'


class TestRunLifecycle:
  """what `run()` emits over the broker channel, and when relative to the trail."""

  @pytest.mark.asyncio
  async def test_terminal_run_emits_started_and_completed(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(response='the result'))
    result = await runner.run('input', tracker=_TrailIDTracker(), surface='test')
    assert result == 'the result'
    assert [m.type for m in transport.sent] == ['progress', 'result']
    assert transport.sent[0].payload == {'trail_id': 'trail-42'}
    assert transport.sent[1].payload == {'outcome': 'ok', 'value': 'the result'}
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_raised_run_emits_completed_with_reason(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(error=BroRaised('cannot fulfill')))
    with pytest.raises(BroRaised):
      await runner.run('input', surface='test')
    assert [m.type for m in transport.sent] == ['progress', 'result']
    assert transport.sent[1].payload == {
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
    assert [m.type for m in transport.sent] == ['progress', 'result']
    assert transport.sent[1].payload == {
      'outcome': 'failed',
      'error': 'kaboom',
      'detail': {'reason': 'error'},
    }
    assert transport.closed is True

  @pytest.mark.asyncio
  async def test_started_follows_start_trail_and_completed_follows_end_trail(self):
    events: list[str] = []

    class OrderTracker(NullTracker):
      def start_trail(
        self,
        bro,
        llm_spec,
        system_prompt,
        forked_from,
        interactive,
        surface,
        hold='unattended',
        summoned_by=None,
      ) -> str:
        events.append('start_trail')
        return 'tid'

      def end_trail(self, reason, detail=None) -> None:
        events.append('end_trail')

    class OrderTransport(_RecordingTransport):
      def send(self, message: Message) -> None:
        events.append(message.type)
        super().send(message)

    transport = OrderTransport()
    runner = _ChannelRunner(BroChannel(Client(transport), 'X'), _StubLLM())
    await runner.run('input', tracker=OrderTracker(), surface='test')
    assert events == ['start_trail', 'progress', 'end_trail', 'result']

  @pytest.mark.asyncio
  async def test_run_without_channel_emits_nothing(self, monkeypatch):
    monkeypatch.delenv(CHANNEL_ENV, raising=False)
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
    result = await runner.run('input', tracker=_TrailIDTracker(), surface='test')
    assert result == 'the answer'
    assert [m.type for m in transport.sent] == ['progress', 'result']
    assert transport.sent[1].payload == {'outcome': 'ok', 'value': 'the answer'}

  @pytest.mark.asyncio
  async def test_summoned_send_announces_started_with_the_workspace(self, monkeypatch):
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    monkeypatch.setenv('RIDE_WORKSPACE', 'my-manual')
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(response='chat'))
    assert await runner.send('hi', tracker=_TrailIDTracker(), surface='test') == 'chat'
    assert [m.type for m in transport.sent] == ['progress']
    assert transport.sent[0].payload == {'trail_id': 'trail-42', 'workspace': 'my-manual'}
    assert transport.closed is True
    # only the conversation's first send announces
    assert await runner.send('more', surface='test') == 'chat'
    assert [m.type for m in transport.sent] == ['progress']

  @pytest.mark.asyncio
  async def test_send_propagates_answer_delivered_to_the_surface(self):
    channel, transport = _make_channel()
    runner = _ChannelRunner(channel, _StubLLM(error=AnswerDelivered('done')))
    with pytest.raises(AnswerDelivered):
      await runner.send('hi', surface='test')
    assert transport.sent == []  # the surface owns the delivery


class TestAgentIdentity:
  def test_create_llm_threads_the_agent_identity(self):
    from bro.llm.llms.echo import LLMSpec as EchoSpec

    class PlainBro(Declared):
      llm_spec = EchoSpec()

    assert Runner(PlainBro(name='plain'))._create_llm(hold='unattended').agent == 'bro//plain'
