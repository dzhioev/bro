import json
import sys
import types
from typing import ClassVar, Optional

import pytest

from bro.bro import BaseBro, BroRaised, set_default_tracker_factory
from bro.bros.ppp_dev import PPPDev
from bro.datasources.searchable import Hit, SearchableDataSource
from llm.llm import LLM
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, describe
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

  def _create_llm(self, *, interactive: bool) -> LLM:
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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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
      def start_trail(self, bro, llm_spec, system_prompt, parent, interactive, entry_point) -> str:
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

      def _create_llm(self, *, interactive: bool):
        return MockLLM(response='ok')

    await TraceBro().run('hello', tracker=RecordingTracker())
    assert [c[0] for c in calls] == ['start', 'end']
    start_kwargs = calls[0][1]
    assert start_kwargs['bro'] == 'trace-bro'
    assert start_kwargs['interactive'] is False
    assert start_kwargs['entry_point'] == 'cli:bro_run'
    assert start_kwargs['parent'] is None
    assert 'base prompt' in start_kwargs['system_prompt']
    assert calls[1][1]['reason'] == 'terminal'

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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
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
      def start_trail(self, bro, llm_spec, system_prompt, parent, interactive, entry_point) -> str:
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

      def _create_llm(self, *, interactive: bool):
        return MockLLM()

    bro = SendBro()
    await bro.send('first', tracker=RecordingTracker())
    await bro.send('second')
    # start_trail fires once — interactive trails span the whole conversation.
    assert [c[0] for c in calls] == ['start']
    assert calls[0][1]['interactive'] is True
    assert calls[0][1]['entry_point'] == 'http'
    assert calls[0][1]['bro'] == 'send-bro'

  @pytest.mark.asyncio
  async def test_send_subsequent_calls_only_user(self):
    llm = MockLLM()

    class CaptureBro(BaseBro):
      name = 'capture'
      description = 'captures'

      def __init__(self):
        super().__init__(system_prompt='be helpful')

      def _create_llm(self, *, interactive: bool):
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

      def _create_llm(self, *, interactive: bool):
        llm = MockLLM()
        llm_instances.append(llm)
        return llm

    bro = TrackBro()
    await bro.run('one-shot')
    await bro.send('first')
    await bro.send('second')
    assert len(llm_instances) == 2


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
  summary = 'base{{#has_cred openai}} query summary on{{else}} no key{{/has_cred}}'

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
    servers = bro._mcp_servers
    assert len(servers) == 1
    assert servers[0].namespace == 'stub-source'
    tools = await servers[0].list_tools()
    tool_names = {t.name for t in tools}
    # local (in-namespace) names; the `stub-source` namespace is applied when the
    # registry forms wire names (`stub-source__search`).
    assert tool_names == {'search', 'fetch'}

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
    # canonical `::` in the data-source block, resolved by the tool-names rule
    assert 'wikipedia-source::search' in bro.system_prompt

  def test_summary_has_cred_rendered_present(self, monkeypatch):
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

  def test_summary_has_cred_rendered_absent(self, monkeypatch):
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
      mcp_servers: ClassVar = [_make_server('a')]

      def __init__(self):
        super().__init__(system_prompt='base')

    prompt = ToolBro().system_prompt
    assert '## Tool names' in prompt
    assert '`namespace::tool`' in prompt
    assert '`namespace__tool`' in prompt
    # generic wording: nothing about a repo/codebase (reaches repo-unaware bros)
    assert 'repo' not in prompt.lower()

  def test_absent_when_bro_has_no_tools_or_skills(self):
    class BareBro(BaseBro):
      name = 'bare'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='base')

    assert '## Tool names' not in BareBro().system_prompt

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

    def fn() -> str:
      return 'ok'

    fn.__name__ = name
    describe(fn, f'{name} tool')
    tools.append(FunctionTool(fn))
  return InProcessMCPServer('test', tools)


class TestBroMcpServers:
  @pytest.mark.asyncio
  async def test_instance_entry_exposes_its_tools(self):
    server = _make_server('a', 'b', 'c')

    class InstanceBro(BaseBro):
      name = 'instance'
      description = 'd'
      mcp_servers: ClassVar = [server]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = InstanceBro()
    assert bro._mcp_servers == [server]
    tools = await bro._mcp_servers[0].list_tools()
    assert {t.name for t in tools} == {'a', 'b', 'c'}

  @pytest.mark.asyncio
  async def test_factory_entry_called_once_per_instance(self):
    calls = 0

    def factory():
      nonlocal calls
      calls += 1
      return _make_server('a')

    class CountBro(BaseBro):
      name = 'count'
      description = 'd'
      mcp_servers: ClassVar = [factory]

      def __init__(self):
        super().__init__(system_prompt='')

    CountBro()
    CountBro()
    assert calls == 2


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
      mcp_servers: ClassVar = [_SecretServer()]
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
    assert set(PPPDev().needed_secrets()) == {'github', 'notion', 'focus'}
    assert set(Assistant().needed_secrets()) == {'notion', 'focus'}
    # PM carries the WebSearch source (brave) for triage lookups; its query-focused
    # fetch summary makes openai an optional (best-effort) secret, not required.
    assert set(PM().needed_secrets()) == {'notion', 'focus', 'brave'}
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
      mcp_servers: ClassVar = [_OptionalServer()]
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
      mcp_servers: ClassVar = [_BothServer()]
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


async def _find_raise_tool(bro: BaseBro):
  for tool in await bro._service_server.list_tools():
    if tool.name == 'raise':
      return tool
  raise AssertionError('raise tool not found on bro service server')


class TestRaise:
  @pytest.mark.asyncio
  async def test_raise_tool_included_in_non_interactive_mode(self):
    bro = EchoBro()
    names = await _collect_tool_names(bro._mcp_servers_for(interactive=False))
    assert 'raise' in names

  @pytest.mark.asyncio
  async def test_raise_tool_excluded_in_interactive_mode(self):
    bro = EchoBro()
    names = await _collect_tool_names(bro._mcp_servers_for(interactive=True))
    assert 'raise' not in names

  @pytest.mark.asyncio
  async def test_raise_tool_raises_bro_raised(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    with pytest.raises(BroRaised) as exc:
      await tool.call({'reason': 'missing api key'})
    assert exc.value.reason == 'missing api key'

  def test_non_interactive_system_prompt_includes_note(self):
    bro = EchoBro()
    prompt = bro._system_prompt_for(interactive=False)
    assert 'non-interactive' in prompt
    assert '`raise`' in prompt
    assert 'unclear' in prompt
    assert bro.system_prompt in prompt

  @pytest.mark.asyncio
  async def test_raise_tool_description_covers_unclear_input(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    assert 'unclear' in tool.description

  def test_interactive_system_prompt_includes_note(self):
    bro = EchoBro()
    prompt = bro._system_prompt_for(interactive=True)
    assert 'non-interactive' not in prompt
    assert 'interactive mode' in prompt
    assert 'clarifying question' in prompt
    assert bro.system_prompt in prompt

  @pytest.mark.asyncio
  async def test_run_creates_llm_in_non_interactive_mode(self):
    captured: list[bool] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, interactive: bool):
        captured.append(interactive)
        return MockLLM()

    await CaptureBro().run('input')
    assert captured == [False]

  @pytest.mark.asyncio
  async def test_send_creates_llm_in_interactive_mode(self):
    captured: list[bool] = []

    class CaptureBro(BaseBro):
      name = 'capture-mode'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

      def _create_llm(self, *, interactive: bool):
        captured.append(interactive)
        return MockLLM()

    await CaptureBro().send('input')
    assert captured == [True]


@pytest.fixture
def fake_pkgs(tmp_path):
  # synthesize ad-hoc packages on disk + sys.modules so test classes can point
  # their `__module__` at one and exercise the skills FS walk without polluting
  # the real `bro/bros/` tree.
  added: list[str] = []

  def make(name: str, skills: Optional[dict[str, str]] = None) -> str:
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    init_path = pkg_dir / '__init__.py'
    init_path.write_text('')
    if skills is not None:
      skills_dir = pkg_dir / 'skills'
      skills_dir.mkdir()
      for skill_name, content in skills.items():
        (skills_dir / f'{skill_name}.md').write_text(content)
    module = types.ModuleType(name)
    module.__file__ = str(init_path)
    sys.modules[name] = module
    added.append(name)
    return name

  yield make

  for name in added:
    sys.modules.pop(name, None)


def _skill(description: str = 'a skill', body: str = 'do the thing') -> str:
  # omit `name:` from the frontmatter — `_load_skill` validates it against the
  # filename stem, and each caller picks its own stem via fake_pkgs.
  return f'---\ndescription: {description}\nversion: 1.0\n---\n\n{body}'


class TestSkillsDiscovery:
  def test_finds_skills_in_class_pkg(self, fake_pkgs):
    pkg = fake_pkgs('_skills_a', {'pr': _skill('open a PR', 'pr body')})

    class SkillBro(BaseBro):
      name = 'sb'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    SkillBro.__module__ = pkg
    bro = SkillBro()
    assert set(bro.skills) == {'pr'}
    assert bro.skills['pr'].read_text().endswith('pr body')

  def test_no_skills_when_pkg_has_no_skills_dir(self, fake_pkgs):
    pkg = fake_pkgs('_skills_b')

    class EmptyBro(BaseBro):
      name = 'eb'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    EmptyBro.__module__ = pkg
    bro = EmptyBro()
    assert bro.skills == {}

  def test_ad_hoc_class_in_non_package_module_has_no_skills(self):
    # tests live in bro/bro_test.py — not an __init__.py — so the walk skips it
    # and discovers no skills regardless of any sibling `skills/` dir.
    class LocalBro(BaseBro):
      name = 'local'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    bro = LocalBro()
    assert bro.skills == {}

  def test_mro_merge_derived_overrides_parent(self, fake_pkgs):
    parent_pkg = fake_pkgs(
      '_skills_parent',
      {
        'pr': _skill('parent pr', 'PARENT_PR_BODY'),
        'land': _skill('land', 'LAND_BODY'),
      },
    )
    child_pkg = fake_pkgs(
      '_skills_child',
      {
        'pr': _skill('child pr', 'CHILD_PR_BODY'),
        'fix': _skill('fix', 'FIX_BODY'),
      },
    )

    class ParentBro(BaseBro):
      name = 'p'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    ParentBro.__module__ = parent_pkg

    class ChildBro(ParentBro):
      name = 'c'
      description = 'd'

    ChildBro.__module__ = child_pkg

    bro = ChildBro()
    skills = bro.skills
    assert set(skills) == {'pr', 'land', 'fix'}
    assert 'CHILD_PR_BODY' in skills['pr'].read_text()
    assert 'LAND_BODY' in skills['land'].read_text()
    assert 'FIX_BODY' in skills['fix'].read_text()


class TestGetSkillBody:
  def test_strips_frontmatter(self, fake_pkgs):
    pkg = fake_pkgs(
      '_get_body',
      {'thing': '---\nname: thing\ndescription: d\nversion: 1\n---\n\n# Head\n\nthe body'},
    )

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    body = B().get_skill_body('thing')
    assert body.startswith('# Head')
    assert 'the body' in body
    assert 'description' not in body
    assert '---' not in body

  def test_no_frontmatter_returns_text_as_is(self, fake_pkgs):
    pkg = fake_pkgs('_get_body_plain', {'plain': 'just text\n'})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    assert B().get_skill_body('plain') == 'just text'

  def test_raises_on_unknown_name(self, fake_pkgs):
    pkg = fake_pkgs('_get_body_unknown', {'known': _skill()})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    with pytest.raises(KeyError) as exc:
      B().get_skill_body('missing')
    msg = str(exc.value)
    assert 'missing' in msg
    assert 'known' in msg


class TestSkillDescriptions:
  def test_returns_name_description_pairs(self, fake_pkgs):
    pkg = fake_pkgs(
      '_descs',
      {
        'a': _skill('first desc'),
        'b': _skill('second desc'),
      },
    )

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    descs = B().skill_descriptions()
    assert sorted(descs) == [('a', 'first desc'), ('b', 'second desc')]

  def test_missing_description_becomes_empty_string(self, fake_pkgs):
    pkg = fake_pkgs(
      '_descs_missing',
      {'lone': '---\nname: lone\n---\nbody'},
    )

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    assert B().skill_descriptions() == [('lone', '')]


class TestSkillsInSystemPrompt:
  def test_section_present_with_skills(self, fake_pkgs):
    pkg = fake_pkgs('_prompt_yes', {'foo': _skill('do foo thing')})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='base prompt')

    B.__module__ = pkg
    prompt = B().system_prompt
    assert '## Available skills' in prompt
    assert '**foo** — do foo thing' in prompt
    assert '`skill` tool' in prompt

  def test_section_omitted_without_skills(self, fake_pkgs):
    pkg = fake_pkgs('_prompt_no')

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='base prompt')

    B.__module__ = pkg
    assert '## Available skills' not in B().system_prompt

  def test_description_truncated_to_first_sentence(self, fake_pkgs):
    long = 'Trigger here. Second sentence with detail. Third sentence.'
    pkg = fake_pkgs('_prompt_trunc', {'foo': _skill(long)})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    prompt = B().system_prompt
    assert '**foo** — Trigger here.' in prompt
    assert 'Second sentence' not in prompt
    assert 'Third sentence' not in prompt


class TestSkillNameValidation:
  def test_mismatched_name_raises(self, fake_pkgs):
    pkg = fake_pkgs('_name_drift', {'real': '---\nname: wrong\n---\n\nbody'})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    with pytest.raises(ValueError) as exc:
      B()
    msg = str(exc.value)
    assert 'wrong' in msg
    assert 'real' in msg

  def test_matching_name_accepted(self, fake_pkgs):
    pkg = fake_pkgs('_name_ok', {'real': '---\nname: real\ndescription: d\n---\n\nbody'})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    bro = B()
    assert bro.skill_descriptions() == [('real', 'd')]

  def test_missing_name_accepted(self, fake_pkgs):
    pkg = fake_pkgs('_name_absent', {'real': '---\ndescription: d\n---\n\nbody'})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    assert B().skill_descriptions() == [('real', 'd')]

  def test_mismatch_caught_by_load_skill_helper(self, tmp_path):
    # `_load_skill` is the shared validation point used by both get_skill_body
    # and skill_descriptions; check it directly so the behavior doesn't depend
    # on the __init__ path.
    from bro.bro import _load_skill

    path = tmp_path / 'real.md'
    path.write_text('---\nname: wrong\n---\n\nbody')
    with pytest.raises(ValueError) as exc:
      _load_skill('real', path)
    msg = str(exc.value)
    assert 'wrong' in msg
    assert 'real' in msg


class TestSkillServiceTool:
  @pytest.mark.asyncio
  async def test_skill_tool_present_when_bro_has_skills(self, fake_pkgs):
    pkg = fake_pkgs('_svc_yes', {'foo': _skill('do foo', 'foo body')})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    tools = await B()._service_server.list_tools()
    names = {t.name for t in tools}
    assert 'raise' in names
    assert 'skill' in names

  @pytest.mark.asyncio
  async def test_skill_tool_absent_when_bro_has_no_skills(self, fake_pkgs):
    pkg = fake_pkgs('_svc_no')

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    tools = await B()._service_server.list_tools()
    names = {t.name for t in tools}
    assert names == {'raise'}

  @pytest.mark.asyncio
  async def test_skill_tool_survives_interactive_mode(self, fake_pkgs):
    # interactive runs drop `raise` (no caller to abort to) but must KEEP `skill`
    # — a skill-having bro driven via `call` still needs to load its skills.
    pkg = fake_pkgs('_svc_interactive', {'foo': _skill()})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    bro = B()
    interactive = await _collect_tool_names(bro._mcp_servers_for(interactive=True))
    non_interactive = await _collect_tool_names(bro._mcp_servers_for(interactive=False))
    assert 'skill' in interactive  # the fix: skill is not dropped along with raise
    assert 'raise' not in interactive  # raise is still dropped interactively
    assert {'skill', 'raise'} <= non_interactive

  @pytest.mark.asyncio
  async def test_skill_tool_returns_body(self, fake_pkgs):
    pkg = fake_pkgs(
      '_svc_call',
      {'foo': '---\ndescription: do foo\n---\n\nthe foo body text'},
    )

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    bro = B()
    tools = await bro._service_server.list_tools()
    skill_tool = next(t for t in tools if t.name == 'skill')
    result = await skill_tool.call({'name': 'foo'})
    assert result == 'the foo body text'

  @pytest.mark.asyncio
  async def test_skill_tool_failure_surfaces_as_string(self, fake_pkgs):
    # FunctionTool's caller (the agent loop) catches generic exceptions and
    # feeds them back as the tool result. KeyError on unknown name must NOT
    # derive from ToolControlSignal — otherwise it would escape the loop.
    pkg = fake_pkgs('_svc_fail', {'known': _skill()})

    class B(BaseBro):
      name = 'b'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    B.__module__ = pkg
    bro = B()
    tools = await bro._service_server.list_tools()
    skill_tool = next(t for t in tools if t.name == 'skill')
    with pytest.raises(KeyError):
      await skill_tool.call({'name': 'missing'})


class TestPPPDevSkillsMRO:
  def test_inherits_dev_skills_and_adds_own(self):
    # `pr` and `land` come from `Dev`'s skills/ via the MRO walk; `fix` is
    # declared in `PPPDev`'s own skills/. All three must surface in the
    # rendered system prompt.
    prompt = PPPDev().system_prompt
    assert '## Available skills' in prompt
    assert '**pr**' in prompt
    assert '**land**' in prompt
    assert '**fix**' in prompt


class TestPersona:
  def test_persona_is_class_prompts_without_shared(self):
    bro = PPPDev()
    # MRO-concatenated class prompts: Dev's contribution + PPPDev's own
    assert 'software developer' in bro.persona
    assert '## PPP project' in bro.persona
    assert "wasn't in the room" in bro.persona
    # shared prompts and the skills block are excluded from persona but present
    # in the full composed system prompt
    assert 'Interaction policy' not in bro.persona
    assert '## Available skills' not in bro.persona
    assert 'Interaction policy' in bro.system_prompt

  def test_persona_honors_explicit_override(self):
    assert EchoBro().persona == 'you echo'
