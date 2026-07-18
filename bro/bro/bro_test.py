import asyncio
import json
import os
import signal
import types
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

import bro.bro
import llm.mcp
from base import credentials
from base.condition import when
from bro.bro import BaseBro, BroRaised, set_default_tracker_factory
from bro.bros.ppp_dev import PPPDev
from bro.datasources.searchable import Hit, SearchableDataSource
from llm.llm import LLM
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, MCPServerSpec, describe
from llm.observer import NullObserver, Observer
from llm.tracker import NullTracker, Tracker


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    self.send_calls.append(messages)
    return self.response


class EchoBro(BaseBro):
  name = 'echo'
  description = 'echoes input'

  def __init__(self, response: str = 'echoed'):
    super().__init__(system_prompt='you echo')
    self._response = response

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, hold: str) -> LLM:
    return MockLLM(response=self._response)


class TestBroRun:
  @pytest.mark.asyncio
  async def test_run_returns_response(self):
    bro = EchoBro(response='hello back')
    result = await bro.run('hello')
    assert result == 'hello back'

  @pytest.mark.asyncio
  async def test_run_wires_observer_through_to_llm(self):
    captured: list[Observer] = []

    class CapturingObserver(NullObserver):
      pass

    class WireBro(BaseBro):
      name = 'wire'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _make_observer(self) -> Observer:
        return CapturingObserver()

      def _create_llm(self, *, hold: str):
        captured.append(self._observer)
        return MockLLM()

    await WireBro().run('hi')
    assert len(captured) == 1
    assert isinstance(captured[0], CapturingObserver)

  @pytest.mark.asyncio
  async def test_run_explicit_observer_overrides_make_observer(self):
    captured: list[Observer] = []

    class MadeObserver(NullObserver):
      pass

    class ExplicitObserver(NullObserver):
      pass

    class OverrideBro(BaseBro):
      name = 'override'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _make_observer(self) -> Observer:
        return MadeObserver()

      def _create_llm(self, *, hold: str):
        captured.append(self._observer)
        return MockLLM()

    explicit = ExplicitObserver()
    await OverrideBro().run('hi', observer=explicit)
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_run_explicit_tracker_overrides_default(self):
    captured: list[Tracker] = []

    class ExplicitTracker(NullTracker):
      pass

    class WireBro(BaseBro):
      name = 'tracker-run'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='hi')

      def _create_llm(self, *, hold: str):
        captured.append(self._tracker)
        return MockLLM()

    explicit = ExplicitTracker()
    await WireBro().run('input', tracker=explicit)
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_run_calls_start_and_end_trail(self):
    calls: list[tuple[str, dict]] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self, bro, llm_spec, system_prompt, parent, interactive, entry_point, summoner=None
      ) -> str:
        calls.append(
          (
            'start',
            {
              'bro': bro,
              'llm_spec': llm_spec,
              'system_prompt': system_prompt,
              'interactive': interactive,
              'entry_point': entry_point,
              'parent': parent,
              'summoner': summoner,
            },
          )
        )
        return 'tid'

      def end_trail(self, reason) -> None:
        calls.append(('end', {'reason': reason}))

    class TraceBro(BaseBro):
      name = 'trace-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='base prompt')

      def _create_llm(self, *, hold: str):
        return MockLLM(response='ok')

    await TraceBro().run('hello', tracker=RecordingTracker())
    assert [c[0] for c in calls] == ['start', 'end']
    start_kwargs = calls[0][1]
    assert start_kwargs['bro'] == 'trace-bro'
    assert start_kwargs['interactive'] is False
    assert start_kwargs['entry_point'] == 'cli:bro_run'
    assert start_kwargs['parent'] is None
    assert start_kwargs['summoner'] is None
    assert 'base prompt' in start_kwargs['system_prompt']
    assert calls[1][1]['reason'] == 'terminal'

  @pytest.mark.asyncio
  async def test_run_passes_summoner_from_the_launch_env(self, monkeypatch):
    captured: list[Optional[dict]] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self, bro, llm_spec, system_prompt, parent, interactive, entry_point, summoner=None
      ) -> str:
        captured.append(summoner)
        return 'tid'

    class TraceBro(BaseBro):
      name = 'trace-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='base prompt')

      def _create_llm(self, *, hold: str):
        return MockLLM(response='ok')

    monkeypatch.setenv('CW_SUMMONER', '{"target":"pm","trail_id":"T-parent"}')
    await TraceBro().run('hello', tracker=RecordingTracker())
    assert captured == [{'target': 'pm', 'trail_id': 'T-parent'}]
    # consumed on read: a nested in-place run spawned by this process's tools
    # must not inherit the marker and re-stamp the parent's summoner
    assert 'CW_SUMMONER' not in os.environ
    await TraceBro().run('again', tracker=RecordingTracker())
    assert captured == [{'target': 'pm', 'trail_id': 'T-parent'}, None]

  @pytest.mark.asyncio
  async def test_run_end_reason_is_raised_on_bro_raised(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def end_trail(self, reason) -> None:
        calls.append(reason)

    class RaiseBro(BaseBro):
      name = 'raise-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        class Boom(MockLLM):
          async def send(self, messages, *, request_timeout=None):
            raise BroRaised('nope')

        return Boom()

    with pytest.raises(BroRaised):
      await RaiseBro().run('x', tracker=RecordingTracker())
    assert calls == ['raised']

  @pytest.mark.asyncio
  async def test_run_end_reason_is_error_on_generic_exception(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def end_trail(self, reason) -> None:
        calls.append(reason)

    class BoomBro(BaseBro):
      name = 'boom-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        class Boom(MockLLM):
          async def send(self, messages, *, request_timeout=None):
            raise RuntimeError('kaboom')

        return Boom()

    with pytest.raises(RuntimeError):
      await BoomBro().run('x', tracker=RecordingTracker())
    assert calls == ['error']

  def test_default_tracker_factory_can_be_swapped(self):
    sentinel = NullTracker()

    class FactoryBro(BaseBro):
      name = 'factory-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    set_default_tracker_factory(lambda: sentinel)
    try:
      assert FactoryBro()._make_tracker() is sentinel
    finally:
      set_default_tracker_factory(NullTracker)

  def test_default_factory_raises_when_trails_config_missing(self, monkeypatch, tmp_path):
    # `_default_factory` refuses to silently fall back to `NullTracker` —
    # missing config is a setup error that must be surfaced.
    from base import credentials
    from bro import bro as bro_module

    monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path))
    monkeypatch.setattr(credentials, 'PPP_DIR', str(tmp_path))
    monkeypatch.setattr(credentials, '_default_store', None)
    monkeypatch.delenv(bro_module._TRAILS_DISABLED_ENV, raising=False)
    with pytest.raises(RuntimeError, match='bootstrap_trails.sh'):
      bro_module._default_factory()

  # presence is what counts (same convention as NO_COLOR / CW_IN_CONTAINER):
  # any value, including '' and '0', enables the switch. unset it to record.
  @pytest.mark.parametrize('value', ['1', '', '0', 'whatever'])
  def test_default_factory_disabled_by_env_var(self, monkeypatch, value):
    # the kill switch wins before the secret check: a missing secret would
    # otherwise raise, but `TRAILS_DISABLED` short-circuits to NullTracker.
    from bro import bro as bro_module

    monkeypatch.setenv(bro_module._TRAILS_DISABLED_ENV, value)
    assert isinstance(bro_module._default_factory(), NullTracker)

  @pytest.mark.asyncio
  async def test_run_passes_system_and_user_messages(self):
    llm = MockLLM()

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures messages'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, hold: str):
        return llm

    bro = CaptureBro()
    await bro.run('test input')
    assert len(llm.send_calls) == 1
    messages = llm.send_calls[0]
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert 'be helpful' in messages[0]['content']
    assert messages[1] == {'role': 'user', 'content': 'test input'}


class TestBroSend:
  @pytest.mark.asyncio
  async def test_send_returns_response(self):
    bro = EchoBro(response='hello')
    result = await bro.send('hi')
    assert result == 'hello'

  @pytest.mark.asyncio
  async def test_send_reuses_llm(self):
    llm_instances = []

    class TrackBro(BaseBro):
      name = 'track'
      description = 'tracks'

      def __init__(self):
        super().__init__(system_prompt='track')

      def _create_llm(self, *, hold: str):
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    bro = TrackBro()
    await bro.send('a')
    await bro.send('b')
    assert len(llm_instances) == 1

  @pytest.mark.asyncio
  async def test_send_first_call_includes_system_prompt(self):
    llm = MockLLM()

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, hold: str):
        return llm

    bro = CaptureBro()
    await bro.send('hi')
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

    class WireBro(BaseBro):
      name = 'wire-send'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        captured.append(self._observer)
        return MockLLM()

    explicit = ExplicitObserver()
    bro = WireBro()
    await bro.send('hi', observer=explicit)
    await bro.send('again')
    assert len(captured) == 1
    assert captured[0] is explicit

  @pytest.mark.asyncio
  async def test_send_first_call_starts_interactive_trail(self):
    calls: list[tuple[str, dict]] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self, bro, llm_spec, system_prompt, parent, interactive, entry_point, summoner=None
      ) -> str:
        calls.append(
          (
            'start',
            {'interactive': interactive, 'entry_point': entry_point, 'bro': bro},
          )
        )
        return 'tid'

    class SendBro(BaseBro):
      name = 'send-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        return MockLLM()

    bro = SendBro()
    await bro.send('first', tracker=RecordingTracker())
    await bro.send('second')
    # start_trail fires once — interactive trails span the whole conversation.
    assert [c[0] for c in calls] == ['start']
    assert calls[0][1]['interactive'] is True
    assert calls[0][1]['entry_point'] == 'send'
    assert calls[0][1]['bro'] == 'send-bro'
    assert bro.trail_id == 'tid'

  @pytest.mark.asyncio
  async def test_send_labels_trail_with_callers_entry_point(self):
    calls: list[str] = []

    class RecordingTracker(NullTracker):
      def start_trail(
        self, bro, llm_spec, system_prompt, parent, interactive, entry_point, summoner=None
      ) -> str:
        calls.append(entry_point)
        return 'tid'

    class SendBro(BaseBro):
      name = 'send-bro'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        return MockLLM()

    bro = SendBro()
    await bro.send('first', tracker=RecordingTracker(), entry_point='call')
    # entry_point labels the trail header, so only the opening send matters
    await bro.send('second', entry_point='ignored')
    assert calls == ['call']

  @pytest.mark.asyncio
  async def test_send_rebinds_observer_on_later_sends(self):
    # a preseeded bro (bro.fork) builds its LLM before the interactive surface
    # exists; the surface's renderer must take effect when attached later.
    class MarkerObserver(NullObserver):
      pass

    class WireBro(BaseBro):
      name = 'wire-send'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        return MockLLM()

    bro = WireBro()
    await bro.send('first')
    assert bro._llm is not None
    attached = MarkerObserver()
    await bro.send('second', observer=attached)
    assert bro._observer is attached
    assert bro._llm.observer is attached

  @pytest.mark.asyncio
  async def test_send_subsequent_calls_only_user(self):
    llm = MockLLM()

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, hold: str):
        return llm

    bro = CaptureBro()
    await bro.send('first')
    await bro.send('second')
    assert len(llm.send_calls) == 2
    messages = llm.send_calls[1]
    assert len(messages) == 1
    assert messages[0] == {'role': 'user', 'content': 'second'}

  @pytest.mark.asyncio
  async def test_run_does_not_affect_send_llm(self):
    llm_instances = []

    class TrackBro(BaseBro):
      name = 'track'
      description = 'tracks'

      def __init__(self):
        super().__init__(system_prompt='track')

      def _create_llm(self, *, hold: str):
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    bro = TrackBro()
    await bro.run('one-shot')
    await bro.send('first')
    await bro.send('second')
    assert len(llm_instances) == 2


class GatedBro(BaseBro):
  name = 'gated'
  description = 'declares secrets for credential-gate tests'
  # two manifest names on top of the default chat_gpt spec's `openai`
  extra_secrets = ('alpha', 'beta')

  def __init__(self):
    super().__init__(system_prompt='gated')
    self.mock_llm = MockLLM(response='ran')

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, hold: str) -> LLM:
    return self.mock_llm


@pytest.mark.credential_gate
class TestCredentialGate:
  """the run-start credential gate: every name in `needed_secrets()` plus
  `llm_spec.needed_secrets()` must resolve before any machinery runs."""

  @pytest.mark.asyncio
  async def test_run_refuses_listing_every_missing_name(self, monkeypatch):
    monkeypatch.setattr(credentials, 'available', lambda name: False)
    gated = GatedBro()
    with pytest.raises(BroRaised, match='missing credentials: alpha, beta, openai'):
      await gated.run('hi')
    assert len(gated.mock_llm.send_calls) == 0

  @pytest.mark.asyncio
  async def test_run_proceeds_when_all_resolve(self, monkeypatch):
    monkeypatch.setattr(credentials, 'available', lambda name: True)
    gated = GatedBro()
    assert await gated.run('hi') == 'ran'

  @pytest.mark.asyncio
  async def test_send_reports_missing_names_in_reply(self, monkeypatch):
    available = {'beta', 'openai'}
    monkeypatch.setattr(credentials, 'available', lambda name: name in available)
    gated = GatedBro()
    reply = await gated.send('hi')
    assert reply == 'gated cannot start: missing credentials: alpha'
    assert len(gated.mock_llm.send_calls) == 0
    # the LLM stays unbuilt, so a later send re-checks the store
    available.add('alpha')
    assert await gated.send('hi') == 'ran'

  def test_missing_secrets_ignores_the_optional_tier(self, monkeypatch):
    import llm.llms.echo

    monkeypatch.setattr(credentials, 'available', lambda name: False)

    class OptionalSource(SearchableDataSource):
      name = 'opt'
      summary = 'declares only an optional secret'
      optional_secrets = ('gamma',)

      async def search(self, query: str, limit: int = 5) -> list[Hit]:
        return []

      async def _fetch_content(self, id: str) -> str:
        return ''

    class OptionalBro(BaseBro):
      name = 'optional'
      description = 'no required secrets'
      llm_spec = llm.llms.echo.LLMSpec()
      data_sources: ClassVar = [OptionalSource()]

    optional_bro = OptionalBro()
    assert optional_bro.optional_secrets() == ('gamma',)
    assert optional_bro.missing_secrets() == ()


class _StubSource(SearchableDataSource):
  name = 'stub'
  summary = 'a stub data source for tests'

  def __init__(self):
    self.fetch_calls: list[tuple[str, Optional[str]]] = []

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return [Hit(id='stub-1', title=f'hit for {query}', snippet='stub snippet')]

  # override the concrete `fetch` to capture both args and skip summarisation —
  # this double verifies the fetch tool routes (id, query) through unchanged.
  async def fetch(self, id: str, query: Optional[str] = None) -> str:
    self.fetch_calls.append((id, query))
    return f'content for {id}'

  async def _fetch_content(self, id: str) -> str:
    return f'content for {id}'


class _MarkerSource(SearchableDataSource):
  name = 'marker'
  summary = 'base{{iff #features contains summary}} query summary on{{else}} no key{{end}}'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def _fetch_content(self, id: str) -> str:
    return ''


class TestBroDataSources:
  @pytest.mark.asyncio
  async def test_data_source_mcp_server_mounted(self):
    class SourceBro(BaseBro):
      name = 'with-source'
      description = 'has a data source'
      data_sources: ClassVar = [_StubSource()]

      def __init__(self):
        super().__init__(system_prompt='hi')

    bro = SourceBro()
    servers = bro._live_mcp_servers()
    assert len(servers) == 1
    assert servers[0].namespace == 'stub-source'
    tools = await servers[0].list_tools()
    tool_names = {t.name for t in tools}
    # local (in-namespace) names; the `stub-source` namespace is applied when the
    # registry forms wire names (`stub-source__search`).
    assert tool_names == {'search', 'fetch'}

  def test_data_sources_concatenate_along_mro(self):
    class ParentSourceBro(BaseBro):
      name = 'parent-sources'
      description = 'd'
      data_sources: ClassVar = [_StubSource()]

      def __init__(self):
        super().__init__(system_prompt='base')

    class ChildSourceBro(ParentSourceBro):
      name = 'child-sources'
      data_sources: ClassVar = [_MarkerSource()]

    bro = ChildSourceBro()
    assert [ds.name for ds in bro._data_sources] == ['stub', 'marker']

  def test_data_source_summary_in_system_prompt(self):
    class SourceBro(BaseBro):
      name = 'summary-bro'
      description = 'd'
      data_sources: ClassVar = [_StubSource()]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = SourceBro()
    assert '## Data sources' in bro.system_prompt
    assert '**stub**' in bro.system_prompt
    assert 'a stub data source for tests' in bro.system_prompt
    # canonical `::` in the data-source block, resolved by the tool-names rule;
    # the example derives from the bro's own first source
    assert 'stub-source::' in bro.system_prompt

  def test_summary_feature_directive_rendered_present(self, monkeypatch):
    from base import credentials

    monkeypatch.setattr(credentials, 'available', lambda name: True)

    class MarkBro(BaseBro):
      name = 'mark-on'
      description = 'd'
      data_sources: ClassVar = [_MarkerSource()]

      def __init__(self):
        super().__init__(system_prompt='base')

    prompt = MarkBro().system_prompt
    assert 'query summary on' in prompt
    assert 'no key' not in prompt
    assert '{{' not in prompt  # markers fully resolved, never leak raw

  def test_summary_feature_directive_rendered_absent(self, monkeypatch):
    from base import credentials

    monkeypatch.setattr(credentials, 'available', lambda name: False)

    class MarkBro(BaseBro):
      name = 'mark-off'
      description = 'd'
      data_sources: ClassVar = [_MarkerSource()]

      def __init__(self):
        super().__init__(system_prompt='base')

    prompt = MarkBro().system_prompt
    assert 'no key' in prompt
    assert 'query summary on' not in prompt


class TestToolNamesBlock:
  def test_present_when_bro_has_tools(self):
    class ToolBro(BaseBro):
      name = 'tooled'
      description = 'd'
      mcp_servers: ClassVar = [_make_spec('a')]

      def __init__(self):
        super().__init__(system_prompt='base')

    prompt = ToolBro().system_prompt
    assert '# Tool names' in prompt
    assert '`namespace::tool`' in prompt
    assert '`namespace__tool`' in prompt
    # generic wording: nothing about a repo/codebase (reaches repo-unaware bros).
    # scoped to the block — the shared prompts ahead of it legitimately contain
    # words like "report" that a bare substring scan would trip on
    tool_names_block = prompt[prompt.index('# Tool names') :]
    assert 'repo' not in tool_names_block.lower()

  def test_present_for_framework_skill_loader(self):
    class BareBro(BaseBro):
      name = 'bare'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = BareBro()
    assert '# Tool names' in bro.system_prompt
    assert '`namespace__tool`' in bro.system_prompt
    assert '`mcp__namespace__tool`' in bro.claude_system_prompt

  def test_claude_flavor_teaches_mcp_wire_form(self):
    class ToolBro(BaseBro):
      name = 'tooled'
      description = 'd'
      mcp_servers: ClassVar = [_make_spec('a')]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = ToolBro()
    assert '`mcp__namespace__tool`' in bro.claude_system_prompt
    assert '`mcp__namespace__tool`' not in bro.system_prompt
    assert '`namespace__tool`' in bro.system_prompt
    # everything before the tool-names rule is shared between the flavors; the
    # grounding block closes the claude flavor only (mcp wire)
    assert bro.claude_system_prompt.startswith(bro.system_prompt.split('# Tool names')[0])
    assert '# Tool grounding' not in bro.system_prompt
    grounding_index = bro.claude_system_prompt.index('# Tool grounding')
    assert grounding_index > bro.claude_system_prompt.index('# Tool names')
    assert bro.claude_system_prompt.endswith('as text.')

  @pytest.mark.asyncio
  async def test_data_source_search_and_fetch_calls(self):
    source = _StubSource()
    server = source.as_mcp_server()
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    search_result = await by_name['search'].call({'query': 'foo'})
    assert isinstance(search_result, str)
    parsed = json.loads(search_result)
    assert parsed[0]['id'] == 'stub-1'
    fetch_result = await by_name['fetch'].call({'id': 'x', 'query': 'why'})
    assert fetch_result == 'content for x'
    assert source.fetch_calls == [('x', 'why')]


def _make_server(*tool_names: str) -> InProcessMCPServer:
  tools = []
  for name in tool_names:

    def function() -> str:
      return 'ok'

    function.__name__ = name
    describe(function, f'{name} tool')
    tools.append(FunctionTool(function))
  return InProcessMCPServer('test', tools)


def _make_spec(*tool_names: str) -> MCPServerSpec:
  return MCPServerSpec(build=lambda: _make_server(*tool_names))


class TestBroMCPServers:
  @pytest.mark.asyncio
  async def test_spec_entry_exposes_its_tools(self):
    class SpecBro(BaseBro):
      name = 'spec'
      description = 'd'
      mcp_servers: ClassVar = [_make_spec('a', 'b', 'c')]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = SpecBro()
    tools = await bro._live_mcp_servers()[0].list_tools()
    assert {t.name for t in tools} == {'a', 'b', 'c'}

  def test_spec_built_lazily_and_once(self):
    calls = 0

    def build():
      nonlocal calls
      calls += 1
      return _make_server('a')

    class CountBro(BaseBro):
      name = 'count'
      description = 'd'
      mcp_servers: ClassVar = [MCPServerSpec(build=build)]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = CountBro()
    # metadata surfaces never build the live server
    bro.needed_secrets()
    assert calls == 0
    first = bro._live_mcp_servers()
    assert calls == 1
    assert bro._live_mcp_servers() is first
    assert calls == 1


class TestToolPackEntries:
  @pytest.mark.asyncio
  async def test_bare_toolset_entry_is_the_full_roster(self):
    toolset = llm.mcp.Toolset('bare-roster')

    @toolset.tool('ping tool')
    def ping() -> str:
      return 'pong'

    class ToolsetBro(BaseBro):
      name = 'toolset-entry'
      description = 'd'
      mcp_servers: ClassVar = [when(llm.mcp.harness == 'bro', toolset)]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ToolsetBro()
    assert len(bro._mcp_specs) == 1
    tools = await bro._live_mcp_servers()[0].list_tools()
    assert {t.name for t in tools} == {'ping'}

  @pytest.mark.asyncio
  async def test_module_entry_resolves_through_its_spec_toolset(self):
    toolset = llm.mcp.Toolset('fake-pack')

    @toolset.tool('ping tool')
    def ping() -> str:
      return 'pong'

    pack = types.ModuleType('_fake_pack')
    vars(pack)['spec'] = toolset

    class ModuleBro(BaseBro):
      name = 'module-entry'
      description = 'd'
      mcp_servers: ClassVar = [pack]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ModuleBro()
    tools = await bro._live_mcp_servers()[0].list_tools()
    assert {t.name for t in tools} == {'ping'}

  def test_module_without_spec_toolset_raises(self):
    class BadPackBro(BaseBro):
      name = 'bad-pack'
      description = 'd'
      mcp_servers: ClassVar = [types.ModuleType('_spec_less')]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(TypeError, match='no Toolset named spec'):
      BadPackBro()


class TestConditionalComponents:
  # a bro instance composes for the bro harness, so `when`-wrapped entries are
  # decided against `#harness = bro` at construction.
  def test_off_harness_server_excluded_and_never_built(self):
    def build():
      raise AssertionError('an unmatched spec must never build')

    class CondBro(BaseBro):
      name = 'cond'
      description = 'd'
      mcp_servers: ClassVar = [when(llm.mcp.harness == 'claude', MCPServerSpec(build=build))]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = CondBro()
    assert bro._mcp_specs == []
    assert bro._live_mcp_servers() == []

  def test_matching_condition_included(self):
    class MatchBro(BaseBro):
      name = 'match'
      description = 'd'
      mcp_servers: ClassVar = [when(llm.mcp.harness == 'bro', _make_spec('a'))]

      def __init__(self):
        super().__init__(system_prompt='')

    assert len(MatchBro()._mcp_specs) == 1

  def test_bool_condition_is_a_constant(self):
    class BoolBro(BaseBro):
      name = 'bool'
      description = 'd'
      mcp_servers: ClassVar = [when(False, _make_spec('a')), _make_spec('b')]

      def __init__(self):
        super().__init__(system_prompt='')

    assert len(BoolBro()._mcp_specs) == 1

  def test_off_harness_data_source_excluded_everywhere(self):
    class CondSourceBro(BaseBro):
      name = 'cond-source'
      description = 'd'
      data_sources: ClassVar = [when(llm.mcp.harness == 'claude', _SecretSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = CondSourceBro()
    assert bro._data_sources == []
    assert '## Data sources' not in bro.system_prompt
    assert bro.needed_secrets() == ()
    assert bro._live_mcp_servers() == []


class TestClaudePersonaServers:
  def _bro(self):
    class PersonaBro(BaseBro):
      name = 'persona'
      description = 'd'
      mcp_servers: ClassVar = [
        when(llm.mcp.harness == 'bro', MCPServerSpec.of(_SecretServer)),
        _make_spec('a'),
      ]
      data_sources: ClassVar = [when(llm.mcp.harness == 'bro', _SecretSource())]

      def __init__(self):
        super().__init__(system_prompt='')

    return PersonaBro()

  def test_serves_only_claude_harness_components(self):
    servers = self._bro().claude_persona_mcp_servers()
    assert [s.namespace for s in servers] == ['test', 'bro']

  def test_service_server_carries_banner_but_not_raise(self):
    names = asyncio.run(_collect_tool_names(self._bro().claude_persona_mcp_servers()))
    # `raise` is gated on the session hold (not unattended here — no BRO_HOLD);
    # the environment facts stay available as `banner`
    assert 'banner' in names
    assert 'raise' not in names

  def test_manifest_is_harness_aware(self):
    bro = self._bro()
    # alpha/beta (the bro-gated server) and gamma (the bro-gated source) are
    # invisible to the claude-harness manifest
    assert set(bro.needed_secrets()) == {'alpha', 'beta', 'gamma'}
    assert bro.needed_secrets(harness='claude') == ()

  def test_real_bro_persona_surfaces(self, monkeypatch):
    from bro.bros.dev import Dev

    # brog's state factory reads the self-contained `brog` secret at build
    monkeypatch.setattr(
      'base.credentials.get_json',
      lambda name: {'backend': 'flow', 'transport': 'http', 'url': 'https://x', 'token': 't'},
    )
    # the dev toolset is bro-harness-only — claude's built-in tools cover it —
    # while the reference FileSources serve every harness
    assert [s.namespace for s in Dev().claude_persona_mcp_servers()] == [
      'dev-style-source',
      'bro',
      'at',
    ]
    assert [s.namespace for s in PPPDev().claude_persona_mcp_servers()] == [
      'brog',
      'dev-style-source',
      'environment-source',
      'template-source',
      'conditions-source',
      'bro',
      'at',
    ]
    assert set(PPPDev().needed_secrets(harness='claude')) == {'github', 'brog'}


class _SecretServer(InProcessMCPServer):
  needed_secrets = ('alpha', 'beta')

  def __init__(self):
    super().__init__('secret', [])


class _SecretSource(SearchableDataSource):
  name = 'secret-src'
  summary = 'src with a secret'
  needed_secrets = ('gamma',)

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def _fetch_content(self, id: str) -> str:
    return ''


class TestNeededSecrets:
  def test_unions_mcp_datasources_and_extra(self):
    class ManifestBro(BaseBro):
      name = 'manifest'
      description = 'd'
      mcp_servers: ClassVar = [MCPServerSpec.of(_SecretServer)]
      data_sources: ClassVar = [_SecretSource()]
      extra_secrets = ('delta',)

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ManifestBro()
    # the llm key is NOT in needed_secrets() — surfaces that run the bro add it
    assert bro.needed_secrets() == ('alpha', 'beta', 'delta', 'gamma')
    assert bro.llm_spec.needed_secrets() == ('openai',)  # default chat_gpt

  def test_extra_secrets_mro_unioned(self):
    class Base(BaseBro):
      name = 'base'
      description = 'd'
      extra_secrets = ('one',)

      def __init__(self):
        super().__init__(system_prompt='')

    class Derived(Base):
      name = 'derived'
      extra_secrets = ('two',)

    assert {'one', 'two'} <= set(Derived().needed_secrets())


class TestMaySummon:
  def test_defaults_to_empty(self):
    class Plain(BaseBro):
      name = 'plain'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    assert Plain()._may_summon == ()

  def test_mro_unioned(self):
    class Base(BaseBro):
      name = 'base'
      description = 'd'
      may_summon = ('one',)

      def __init__(self):
        super().__init__(system_prompt='')

    class Derived(Base):
      name = 'derived'
      may_summon = ('two',)

    assert Derived()._may_summon == ('one', 'two')

  def test_empty_when_no_components_and_keyless_llm(self):
    import llm.llms.echo

    class Bare(BaseBro):
      name = 'bare'
      description = 'd'
      llm_spec = llm.llms.echo.LLMSpec()

      def __init__(self):
        super().__init__(system_prompt='')

    assert Bare().needed_secrets() == ()

  def test_real_bro_manifests(self):
    from bro.bros.assistant import Assistant
    from bro.bros.devoops import Devoops
    from bro.bros.librorian import Librorian
    from bro.bros.pm import PM

    # component manifest only (no llm key). the full-toolset flow bros hold the
    # focus tools, so `focus` must be present — exact-set checks (not `<=`) so an
    # under-declaration like B1 can't slip through.
    assert set(PPPDev().needed_secrets()) == {'github', 'brog'}
    assert set(Assistant().needed_secrets()) == {'notion', 'focus'}
    # PM carries the WebSearch source (brave) for triage lookups; its query-focused
    # fetch summary makes openai an optional (best-effort) secret, not required.
    assert set(PM().needed_secrets()) == {'notion', 'focus', 'brave', 'brog'}
    assert PM().optional_secrets() == ('openai',)
    # librorian scopes flow to non-focus tools, so it must NOT pull in `focus`.
    assert 'focus' not in set(Librorian().needed_secrets())
    assert {'tmdb', 'brave', 'notion'} <= set(Librorian().needed_secrets())
    assert 'openai' not in PPPDev().needed_secrets()
    # devoops adds a task-scoped flow server (non-focus tools → `notion`); `focus`
    # still comes from its infra server.
    assert set(Devoops().needed_secrets()) == {'aws', 'infra', 'focus', 'notion'}


class _OptionalServer(InProcessMCPServer):
  needed_secrets = ('alpha',)
  optional_secrets = ('omega',)

  def __init__(self):
    super().__init__('optional-srv', [])


class _OptionalSource(SearchableDataSource):
  name = 'optional-src'
  summary = 'src with an optional secret'
  optional_secrets = ('psi',)

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def _fetch_content(self, id: str) -> str:
    return ''


class TestOptionalSecrets:
  def test_unions_mcp_and_datasource_optional(self):
    class OptBro(BaseBro):
      name = 'opt'
      description = 'd'
      mcp_servers: ClassVar = [MCPServerSpec.of(_OptionalServer)]
      data_sources: ClassVar = [_OptionalSource()]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = OptBro()
    assert bro.optional_secrets() == ('omega', 'psi')

  def test_required_wins_over_optional(self):
    # a secret declared both required (by one component) and optional (by another)
    # stays required-only — never downgraded to best-effort.
    class _BothServer(InProcessMCPServer):
      needed_secrets = ('shared',)

      def __init__(self):
        super().__init__('both-srv', [])

    class _OptShared(SearchableDataSource):
      name = 'opt-shared'
      summary = 's'
      optional_secrets = ('shared',)

      async def search(self, query: str, limit: int = 5) -> list[Hit]:
        return []

      async def _fetch_content(self, id: str) -> str:
        return ''

    class BothBro(BaseBro):
      name = 'both'
      description = 'd'
      mcp_servers: ClassVar = [MCPServerSpec.of(_BothServer)]
      data_sources: ClassVar = [_OptShared()]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = BothBro()
    assert 'shared' in bro.needed_secrets()
    assert bro.optional_secrets() == ()


class TestNeedsDocker:
  def test_default_is_false(self):
    class Plain(BaseBro):
      name = 'plain'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    assert Plain().needs_docker is False

  def test_real_bros(self):
    from bro.bros.devoops import Devoops
    from bro.bros.librorian import Librorian

    # only the deployer declares it; other bros (incl. ppp-dev) do not
    assert Devoops().needs_docker is True
    assert PPPDev().needs_docker is False
    assert Librorian().needs_docker is False


async def _collect_tool_names(servers):
  names: set[str] = set()
  for server in servers:
    for tool in await server.list_tools():
      names.add(tool.name)
  return names


async def _find_tool(bro: BaseBro, name: str):
  for candidate in await bro._service_server.list_tools():
    if candidate.name == name:
      return candidate
  raise AssertionError(f'no {name!r} tool on the service server')


async def _find_raise_tool(bro: BaseBro):
  for tool in await bro._service_server.list_tools():
    if tool.name == 'raise':
      return tool
  raise AssertionError('raise tool not found on bro service server')


class TestRaise:
  @pytest.mark.asyncio
  async def test_raise_tool_included_in_non_interactive_mode(self):
    bro = EchoBro()
    names = await _collect_tool_names(bro._mcp_servers_for(hold='unattended'))
    assert 'raise' in names

  @pytest.mark.asyncio
  async def test_raise_tool_excluded_at_every_other_hold(self):
    bro = EchoBro()
    for hold in ('detached', 'attended', 'guided'):
      names = await _collect_tool_names(bro._mcp_servers_for(hold=hold))
      assert 'raise' not in names

  @pytest.mark.asyncio
  async def test_raise_tool_raises_bro_raised(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    with pytest.raises(BroRaised) as exception:
      await tool.call({'reason': 'missing api key'})
    assert exception.value.reason == 'missing api key'


class TestClaudeRaise:
  """the mcp flavor of `raise`: mounted for unattended claude sessions, records
  the abort over the broker channel and terminates the session through the
  runner (no exception can abort the consuming harness)."""

  def test_unattended_claude_builds_mount_raise(self, monkeypatch):
    monkeypatch.setenv('BRO_HOLD', 'unattended')
    monkeypatch.setenv('CW_RUNNER_PID', '4242')
    bro = EchoBro()
    assert 'raise' in asyncio.run(_collect_tool_names(bro.claude_persona_mcp_servers()))
    assert 'raise' in asyncio.run(_collect_tool_names(bro.claude_bro_mcp_servers()))

  def test_hold_alone_does_not_mount_raise(self, monkeypatch):
    # no runner pid means nothing to terminate — no tool
    monkeypatch.setenv('BRO_HOLD', 'unattended')
    names = asyncio.run(_collect_tool_names(EchoBro().claude_persona_mcp_servers()))
    assert 'raise' not in names

  def test_other_skip_permission_holds_do_not_mount_raise(self, monkeypatch):
    # detached and attended sessions have a human to report to — no abort tool
    monkeypatch.setenv('CW_RUNNER_PID', '4242')
    for hold in ('detached', 'attended', 'guided'):
      monkeypatch.setenv('BRO_HOLD', hold)
      names = asyncio.run(_collect_tool_names(EchoBro().claude_persona_mcp_servers()))
      assert 'raise' not in names

  async def _mcp_raise_tool(self):
    server = bro.bro._build_service_server(EchoBro(), include_raise=True, wire='mcp')
    for tool in await server.list_tools():
      if tool.name == 'raise':
        return tool
    raise AssertionError('raise tool not found on the mcp service build')

  @pytest.mark.asyncio
  async def test_mcp_raise_records_channel_and_kills_the_runner(self, monkeypatch):
    monkeypatch.setenv('CW_RUNNER_PID', '4242')
    channel = MagicMock()
    monkeypatch.setattr('bro.bro.BroChannel.from_env', lambda: channel)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._mcp_raise_tool()
    await tool.call({'reason': 'missing api key'})
    channel.completed.assert_called_once_with('missing api key', 'raised')
    channel.close.assert_called_once_with()
    assert kills == [(4242, signal.SIGTERM)]

  @pytest.mark.asyncio
  async def test_mcp_raise_kills_without_a_channel(self, monkeypatch):
    monkeypatch.setenv('CW_RUNNER_PID', '4242')
    monkeypatch.setattr('bro.bro.BroChannel.from_env', lambda: None)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._mcp_raise_tool()
    await tool.call({'reason': 'no tool fits'})
    assert kills == [(4242, signal.SIGTERM)]

  @pytest.mark.asyncio
  async def test_mcp_raise_kills_even_when_the_channel_emission_fails(self, monkeypatch):
    monkeypatch.setenv('CW_RUNNER_PID', '4242')
    channel = MagicMock()
    channel.completed.side_effect = ConnectionError('channel closed')
    monkeypatch.setattr('bro.bro.BroChannel.from_env', lambda: channel)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._mcp_raise_tool()
    with pytest.raises(ConnectionError):
      await tool.call({'reason': 'broker down'})
    assert kills == [(4242, signal.SIGTERM)]

  @pytest.mark.asyncio
  async def test_raise_description_forks_on_wire(self):
    mcp_tool = await self._mcp_raise_tool()
    bare_tool = await _find_raise_tool(EchoBro())
    assert 'terminates the session' in mcp_tool.description
    assert 'terminates the session' not in bare_tool.description


class TestSessionModePrompts:
  def test_non_interactive_runs_pin_the_unattended_hold(self):
    bro = EchoBro()
    prompt = bro._system_prompt_for(hold='unattended')
    assert '`bro::raise`' in prompt
    assert 'unclear' in prompt
    assert bro.system_prompt in prompt
    assert '# Unattended session' in prompt
    assert '# Guided session' not in prompt
    # the fragment renders at run start — no directive may leak
    assert '{{' not in prompt

  @pytest.mark.asyncio
  async def test_raise_tool_description_covers_unclear_input(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    assert 'unclear' in tool.description

  def test_interactive_runs_pin_the_guided_hold(self):
    bro = EchoBro()
    prompt = bro._system_prompt_for(hold='guided')
    assert 'clarifying question' in prompt
    assert bro.system_prompt in prompt
    assert '# Guided session' in prompt
    assert '# Unattended session' not in prompt


class TestBannerTool:
  @pytest.mark.asyncio
  async def test_present_on_both_service_builds(self):
    bro = EchoBro()
    non_interactive = await _collect_tool_names(bro._mcp_servers_for(hold='unattended'))
    interactive = await _collect_tool_names(bro._mcp_servers_for(hold='guided'))
    assert 'banner' in non_interactive
    assert 'banner' in interactive

  @pytest.mark.asyncio
  async def test_renders_the_llm_banner_with_the_bro_name(self, monkeypatch):
    import workspace.banner

    captured: dict = {}

    def fake_render_banner(llm=False, bro=None):
      captured['llm'] = llm
      captured['bro'] = bro
      return 'kind: container'

    monkeypatch.setattr(workspace.banner, 'render_banner', fake_render_banner)
    tool = await _find_tool(EchoBro(), 'banner')
    assert await tool.call({}) == 'kind: container'
    assert captured == {'llm': True, 'bro': 'echo'}


class _FakeSummonClient:
  """stands in for summon.open_client(): records the close the tool owes it."""

  def __init__(self):
    self.closed = False

  def close(self, confirm: bool = False) -> None:
    del confirm
    self.closed = True


class TestSummonTool:
  @pytest.mark.asyncio
  async def test_absent_without_a_channel(self):
    # conftest drops BROKER_CHANNEL, so the plain construction has no channel
    bro = EchoBro()
    names = await _collect_tool_names([bro._service_server])
    assert 'summon' not in names
    assert 'summon_check' not in names

  @pytest.mark.asyncio
  async def test_present_on_both_service_builds_when_a_channel_is_set(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    bro = EchoBro()
    non_interactive = await _collect_tool_names(bro._mcp_servers_for(hold='unattended'))
    interactive = await _collect_tool_names(bro._mcp_servers_for(hold='guided'))
    # interactive surfaces (`call`) summon too — only `raise` is non-interactive-only
    assert {'summon', 'summon_check'} <= set(non_interactive)
    assert {'summon', 'summon_check'} <= set(interactive)

  @pytest.mark.asyncio
  async def test_summon_list_needs_the_status_file_env(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    monkeypatch.delenv(summon_module.STATUS_ENV, raising=False)
    names = await _collect_tool_names(EchoBro()._mcp_servers_for(hold='unattended'))
    assert 'summon_list' not in names
    monkeypatch.setenv(summon_module.STATUS_ENV, '/anywhere/ws.status.json')
    names = await _collect_tool_names(EchoBro()._mcp_servers_for(hold='unattended'))
    assert 'summon_list' in names

  @pytest.mark.asyncio
  async def test_summon_list_returns_the_recorded_status(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    monkeypatch.setenv(summon_module.STATUS_ENV, '/anywhere/ws.status.json')
    status = {'active': [], 'last': {'request_id': 'R1', 'outcome': 'terminal'}}
    monkeypatch.setattr(summon_module, 'list_summons', lambda: status)
    tool = await _find_tool(EchoBro(), 'summon_list')
    assert await tool.call({}) == status

  @pytest.mark.asyncio
  async def test_calls_summon_and_wait_off_loop(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    calls: list = []
    client = _FakeSummonClient()

    def fake_summon_and_wait(target, prompt, *, timeout=None, into=None, hold=None, client=None):
      calls.append(
        {'target': target, 'prompt': prompt, 'timeout': timeout, 'into': into, 'client': client}
      )
      return 'the answer'

    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'summon_and_wait', fake_summon_and_wait)
    bro = EchoBro()
    tool = None
    for candidate in await bro._service_server.list_tools():
      if candidate.name == 'summon':
        tool = candidate
    assert tool is not None
    result = await tool.call({'target': 'devoops', 'prompt': 'deploy', 'timeout': 60})
    assert result == 'the answer'
    assert calls == [
      {'target': 'devoops', 'prompt': 'deploy', 'timeout': 60, 'into': None, 'client': client}
    ]
    assert client.closed  # the per-call client is closed on the way out

  @pytest.mark.asyncio
  async def test_detach_returns_the_request_id_without_waiting(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    calls: list = []

    def fake_summon_detached(target, prompt, *, timeout=None, into=None, hold=None):
      calls.append({'target': target, 'prompt': prompt, 'timeout': timeout, 'into': into})
      return 'REQ-ID'

    def fail_summon_and_wait(*args, **kwargs):
      raise AssertionError('detach must not block on summon_and_wait')

    monkeypatch.setattr(summon_module, 'summon_detached', fake_summon_detached)
    monkeypatch.setattr(summon_module, 'summon_and_wait', fail_summon_and_wait)
    tool = await _find_tool(EchoBro(), 'summon')
    result = await tool.call({'target': 'devoops', 'prompt': 'deploy', 'detach': True})
    assert result == 'REQ-ID'
    assert calls == [{'target': 'devoops', 'prompt': 'deploy', 'timeout': None, 'into': None}]

  @pytest.mark.asyncio
  async def test_check_reports_pending_and_completed(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    statuses = [
      summon_module.SummonStatus(pending=True, trail_id='T1'),
      summon_module.SummonStatus(pending=False, answer='pong', trail_id='T1'),
    ]
    monkeypatch.setattr(
      summon_module, 'check_summon', lambda request_id, *, last_seen=None: statuses.pop(0)
    )
    tool = await _find_tool(EchoBro(), 'summon_check')
    assert await tool.call({'request_id': 'REQ-1'}) == {'state': 'pending', 'trail_id': 'T1'}
    assert await tool.call({'request_id': 'REQ-1'}) == {'state': 'completed', 'answer': 'pong'}

  @pytest.mark.asyncio
  async def test_check_passes_last_seen_and_reports_the_cursor(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    calls: list = []

    def fake_check_summon(request_id, *, last_seen=None):
      calls.append({'request_id': request_id, 'last_seen': last_seen})
      return summon_module.SummonStatus(pending=False, answer='ok', trail_id='T1', seq=2)

    monkeypatch.setattr(summon_module, 'check_summon', fake_check_summon)
    tool = await _find_tool(EchoBro(), 'summon_check')
    result = await tool.call({'request_id': 'REQ-1', 'last_seen': 0})
    assert result == {'state': 'completed', 'answer': 'ok', 'seq': 2}
    assert calls == [{'request_id': 'REQ-1', 'last_seen': 0}]

  @pytest.mark.asyncio
  async def test_check_reports_collected_with_a_reread_hint(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    status = summon_module.SummonStatus(pending=False, collected=True, seq=2)
    monkeypatch.setattr(summon_module, 'check_summon', lambda request_id, *, last_seen=None: status)
    tool = await _find_tool(EchoBro(), 'summon_check')
    result = await tool.call({'request_id': 'REQ-1'})
    assert isinstance(result, dict)
    assert result['state'] == 'collected'
    assert result['seq'] == 2
    assert 'last_seen' in result['hint']

  @pytest.mark.asyncio
  async def test_check_wait_with_last_seen_is_an_error(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    tool = await _find_tool(EchoBro(), 'summon_check')
    with pytest.raises(ValueError, match='last_seen'):
      await tool.call({'request_id': 'REQ-1', 'wait': True, 'last_seen': 0})

  @pytest.mark.asyncio
  async def test_cancelled_blocking_summon_closes_its_client(self, monkeypatch):
    # the client-side abort path: cancelling the tool call (the MCP client timed
    # out or aborted) must close the per-call channel client, which unblocks the
    # worker thread and detaches the broxy route
    import threading

    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    client = _FakeSummonClient()
    entered = threading.Event()
    release = threading.Event()

    def fake_summon_and_wait(target, prompt, *, timeout=None, into=None, hold=None, client=None):
      entered.set()
      release.wait(timeout=5)
      raise summon_module.SummonError('broker channel closed awaiting the summon result')

    def fake_close(confirm: bool = False) -> None:
      del confirm
      client.closed = True
      release.set()

    monkeypatch.setattr(client, 'close', fake_close)
    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'summon_and_wait', fake_summon_and_wait)
    tool = await _find_tool(EchoBro(), 'summon')
    task = asyncio.create_task(tool.call({'target': 'devoops', 'prompt': 'deploy'}))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert client.closed

  @pytest.mark.asyncio
  async def test_transport_caution_only_on_the_mcp_wire(self, monkeypatch):
    # wire 'mcp' builds are consumed over an MCP transport with a client-side
    # call budget; their summon descriptions carry the timeout caution
    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    bro_instance = EchoBro()
    mcp_build = bro.bro._build_service_server(bro_instance, include_raise=False, wire='mcp')
    bare_build = bro.bro._build_service_server(bro_instance, include_raise=False, wire='bare')
    mcp_tools = {t.name: t for t in await mcp_build.list_tools()}
    bare_tools = {t.name: t for t in await bare_build.list_tools()}
    for name in ('summon', 'summon_check'):
      assert 'CAUTION' in mcp_tools[name].description
      assert 'CAUTION' not in bare_tools[name].description

  @pytest.mark.asyncio
  async def test_check_wait_collects(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    calls: list = []
    client = _FakeSummonClient()

    def fake_collect_summon(request_id, *, timeout=None, on_started=None, client=None):
      calls.append({'request_id': request_id, 'timeout': timeout, 'client': client})
      return 'collected'

    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'collect_summon', fake_collect_summon)
    tool = await _find_tool(EchoBro(), 'summon_check')
    result = await tool.call({'request_id': 'REQ-1', 'wait': True, 'timeout': 60})
    assert result == {'state': 'completed', 'answer': 'collected'}
    assert calls == [{'request_id': 'REQ-1', 'timeout': 60, 'client': client}]
    assert client.closed

  @pytest.mark.asyncio
  async def test_check_timeout_without_wait_is_an_error(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')
    tool = await _find_tool(EchoBro(), 'summon_check')
    with pytest.raises(ValueError, match='wait'):
      await tool.call({'request_id': 'REQ-1', 'timeout': 60})

  @pytest.mark.asyncio
  async def test_summon_failure_propagates_as_the_tool_error(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/run/broker.sock')

    def fake_summon_and_wait(target, prompt, *, timeout=None, into=None, hold=None, client=None):
      raise summon_module.SummonError('summon denied: no')

    monkeypatch.setattr(summon_module, 'open_client', lambda: _FakeSummonClient())
    monkeypatch.setattr(summon_module, 'summon_and_wait', fake_summon_and_wait)
    bro = EchoBro()
    tool = None
    for candidate in await bro._service_server.list_tools():
      if candidate.name == 'summon':
        tool = candidate
    assert tool is not None
    # a generic exception is the agent-loop tool-error contract (vs ToolControlSignal)
    with pytest.raises(summon_module.SummonError, match='summon denied'):
      await tool.call({'target': 'devoops', 'prompt': 'deploy'})

  @pytest.mark.asyncio
  async def test_run_creates_llm_with_the_unattended_hold(self):
    captured: list[str] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        captured.append(hold)
        return MockLLM()

    await CaptureBro().run('input')
    assert captured == ['unattended']

  @pytest.mark.asyncio
  async def test_run_hold_override_reaches_the_llm_build(self):
    captured: list[str] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        captured.append(hold)
        return MockLLM()

    await CaptureBro().run('input', hold='attended')
    assert captured == ['attended']

  @pytest.mark.asyncio
  async def test_send_creates_llm_with_the_guided_hold(self):
    captured: list[str] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, hold: str):
        captured.append(hold)
        return MockLLM()

    await CaptureBro().send('input')
    assert captured == ['guided']


class TestPersona:
  def test_persona_is_class_prompts_without_shared(self):
    bro = PPPDev()
    # a heading names the segment, then the MRO-concatenated class prompts:
    # Dev's contribution + PPPDev's own
    assert bro.persona.startswith('# Persona: ppp-dev')
    assert 'software developer' in bro.persona
    assert '## PPP project' in bro.persona
    assert 'dev-style-source::read' in bro.persona
    # shared prompts and the scripts block are excluded from persona but present
    # in the full composed system prompt
    assert 'Interaction policy' not in bro.persona
    assert '## Scripts' not in bro.persona
    assert 'Interaction policy' in bro.system_prompt

  def test_persona_honors_explicit_override(self):
    assert EchoBro().persona == '# Persona: echo\n\nyou echo'


class TestAgentIdentity:
  def test_agent_namespaces_the_bro_name(self):
    assert EchoBro().agent == 'bro//echo'

  def test_create_llm_threads_the_agent_identity(self):
    from llm.llms.echo import LLMSpec as EchoSpec

    class PlainBro(BaseBro):
      name = 'plain'
      description = 'd'
      llm_spec = EchoSpec()

      def __init__(self):
        super().__init__(system_prompt='')

    assert PlainBro()._create_llm(hold='unattended').agent == 'bro//plain'
