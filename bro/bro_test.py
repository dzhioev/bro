import asyncio
import contextlib
import json
from pathlib import Path
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

import bro.bro as bro_module
import bro.llm.llms.echo as llm_llms_echo
import bro.mcp as mcp
import bro.workspace.banner as workspace_banner
from bro import watches
from bro.base import credentials
from bro.base.condition import ConditionError, iff, when
from bro.bro import BaseBro, feature
from bro.broker.environment import BROKER_TALK
from bro.datasources.file import FileSource
from bro.datasources.man import ManPage, ManSource
from bro.datasources.searchable import Hit, SearchableDataSource
from bro.harness import Harness, SessionEndReason, claude
from bro.llm.mcp import FunctionTool, InProcessMCPServer, MCPServer
from bro.llm.tracker import ToolStepSource
from bro.mcp import MCPServerSpec, describe
from bro.summon import LAUNCH_ENV, SUMMONED_ENV, encode_launch


class EchoBro(BaseBro):
  name = 'echo'
  description = 'echoes input'

  def __init__(self):
    super().__init__(system_prompt='you echo')


class StubRun:
  """the `LiveRun` an assembly test binds the service tools to."""

  def __init__(self, trail_id: Optional[str] = None, tool_step: Optional[ToolStepSource] = None):
    self.trail_id = trail_id
    self.current_tool_step_id = tool_step
    self.watch_store: watches.Store = MagicMock(spec=watches.Store)


def _native_servers(
  bro: BaseBro, *, hold: str = 'unattended', run: Optional[StubRun] = None
) -> list[MCPServer]:
  return bro.assemble(
    harness='bro',
    include_raise=hold == 'unattended',
    live_run=run if run is not None else StubRun(),
  )


def _service_server(
  bro: BaseBro, *, run: Optional[StubRun] = None, harness: mcp.HarnessLike = 'bro'
) -> MCPServer:
  return bro_module._build_service_server(
    bro,
    include_raise=True,
    harness=harness,
    live_run=run if run is not None else StubRun(),
  )


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

  def test_man_pages_fold_into_one_manual_along_the_mro(self):
    page = FileSource('alpha', summary='the alpha page', path=Path(__file__))
    other = FileSource('beta', summary='the beta page', path=Path(__file__))

    class ParentManBro(BaseBro):
      name = 'parent-man'
      description = 'd'
      data_sources: ClassVar = [ManPage(page), _StubSource()]

      def __init__(self):
        super().__init__(system_prompt='base')

    class ChildManBro(ParentManBro):
      name = 'child-man'
      # the repeat collapses; a namespace is one server either way
      data_sources: ClassVar = [ManPage(other), ManPage(page)]

    bro = ChildManBro()
    # the manual sits where the first page was declared, ahead of the stub
    assert [ds.name for ds in bro._data_sources] == ['man', 'stub']
    folded = bro._data_sources[0]
    assert isinstance(folded, ManSource)
    assert [p.name for p in folded.pages] == ['alpha', 'beta']

  def test_man_pages_gate_on_conditions_like_any_source(self):
    page = FileSource('alpha', summary='the alpha page', path=Path(__file__))

    class GatedManBro(BaseBro):
      name = 'gated-man'
      description = 'd'
      data_sources: ClassVar = [when(mcp.harness == 'claude', ManPage(page))]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = GatedManBro()
    assert bro._data_sources == []
    assert [ds.name for ds in bro._components_for('claude')[1]] == ['man']

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
    from bro.base import credentials

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
    from bro.base import credentials

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
      tools: ClassVar = [_make_layer('a')]

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
    assert '`mcp__namespace__tool`' not in bro.system_prompt

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


def _server_layer(server_spec: MCPServerSpec) -> mcp.ToolLayer:
  return mcp.ToolLayer(server_specs=(server_spec,))


def _make_layer(*tool_names: str) -> mcp.ToolLayer:
  return _server_layer(MCPServerSpec(build=lambda: _make_server(*tool_names)))


class TestComponentDeclarations:
  def test_retired_tool_attribute_names_its_replacement(self):
    with pytest.raises(
      TypeError,
      match=r"RetiredBro\.mcp_servers.*move them to 'tools'.*'mcp_servers' was renamed to 'tools'",
    ):

      class RetiredBro(BaseBro):
        mcp_servers: ClassVar = [_make_layer('a')]

  def test_tool_layer_under_typo_raises_at_class_definition(self):
    with pytest.raises(TypeError, match=r"TypoBro\.toolss.*move them to 'tools'"):

      class TypoBro(BaseBro):
        toolss = when(False, _make_layer('a'))

  def test_iff_tool_entry_under_unknown_attribute_raises(self):
    with pytest.raises(TypeError, match=r"ConditionalTypoBro\.tool.*move them to 'tools'"):

      class ConditionalTypoBro(BaseBro):
        tool = iff(False, _make_layer('a'), _make_layer('b'))

  def test_data_source_under_typo_raises_at_class_definition(self):
    with pytest.raises(TypeError, match=r"SourceTypoBro\.datasources.*move them to 'data_sources'"):

      class SourceTypoBro(BaseBro):
        datasources: ClassVar = [when(False, _StubSource())]

  def test_unrelated_helper_attributes_remain_valid(self):
    class HelperBro(BaseBro):
      labels: ClassVar = ['first', 'second']
      lookup: ClassVar = {'first': 1}

    assert HelperBro.labels == ['first', 'second']


class TestBroMCPServers:
  @pytest.mark.asyncio
  async def test_spec_entry_exposes_its_tools(self):
    class SpecBro(BaseBro):
      name = 'spec'
      description = 'd'
      tools: ClassVar = [_make_layer('a', 'b', 'c')]

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
      tools: ClassVar = [_server_layer(MCPServerSpec(build=build))]

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
  async def test_explicit_toolset_spec_is_the_full_roster(self):
    toolset = mcp.Toolset('full-roster')

    @toolset.tool('ping tool')
    def ping() -> str:
      return 'pong'

    class ToolsetBro(BaseBro):
      name = 'toolset-entry'
      description = 'd'
      tools: ClassVar = [when(mcp.harness == 'bro', mcp.mount(toolset))]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ToolsetBro()
    assert len(bro._mcp_specs) == 1
    tools = await bro._live_mcp_servers()[0].list_tools()
    assert {tool.name for tool in tools} == {'ping'}


class TestToolLayers:
  def test_grouped_names_and_mro_entries_compose(self):
    class Base(BaseBro):
      name = 'base-block'
      description = 'd'
      tools: ClassVar = [when(mcp.harness == 'claude', mcp.block('Read', 'Write'))]

      def __init__(self):
        super().__init__(system_prompt='')

    class Derived(Base):
      name = 'derived-block'
      tools: ClassVar = [when(mcp.harness == 'claude', mcp.block('Bash', 'Read'))]

    bro = Derived()
    assert bro.blocked_tool_names('bro') == ()
    assert bro.blocked_tool_names('claude') == ('Read', 'Write', 'Bash')

  def test_iff_can_choose_mounts_or_blocks(self):
    class ConditionalBro(BaseBro):
      name = 'conditional-block'
      description = 'd'
      tools: ClassVar = [
        iff(
          mcp.harness == 'claude',
          mcp.block('Read'),
          _make_layer('read'),
        )
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ConditionalBro()
    assert len(bro._mcp_specs) == 1
    assert bro.blocked_tool_names('claude') == ('Read',)

  def test_block_selected_for_bro_harness_raises(self):
    class InvalidBro(BaseBro):
      name = 'invalid-block'
      description = 'd'
      tools: ClassVar = [mcp.block('Read')]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match="cannot declare native tools.*'bro' harness"):
      InvalidBro()

  def test_narrowing_serves_the_tool_it_takes_out_of_the_block(self):
    class WatchingBro(BaseBro):
      name = 'watching'
      description = 'd'
      tools: ClassVar = [
        when(mcp.harness == 'claude', mcp.block('Bash', 'Monitor')),
        when(mcp.harness == 'claude', mcp.allow_commands('Monitor', 'watch it')),
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = WatchingBro()
    assert bro.blocked_tool_names('claude') == ('Bash',)
    assert bro.narrowed_tool_commands('claude') == {'Monitor': ('watch it',)}
    assert bro.narrowed_tool_commands('bro') == {}

  def test_narrowing_layers_accumulate_their_commands(self):
    class WatchingBro(BaseBro):
      name = 'watching-twice'
      description = 'd'
      tools: ClassVar = [
        when(mcp.harness == 'claude', mcp.block('Monitor')),
        when(mcp.harness == 'claude', mcp.allow_commands('Monitor', 'watch one')),
        when(mcp.harness == 'claude', mcp.allow_commands('Monitor', 'watch two')),
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    assert WatchingBro().narrowed_tool_commands('claude') == {'Monitor': ('watch one', 'watch two')}

  def test_narrowing_a_tool_the_bro_never_blocked_raises(self):
    class InvalidBro(BaseBro):
      name = 'invalid-narrowing'
      description = 'd'
      tools: ClassVar = [when(mcp.harness == 'claude', mcp.allow_commands('Monitor', 'go'))]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match='Monitor is narrowed.*never blocked'):
      InvalidBro().blocked_tool_names('claude')

  def test_serving_takes_a_tool_out_of_the_block_unnarrowed(self):
    class WatchingBro(BaseBro):
      name = 'serving'
      description = 'd'
      tools: ClassVar = [
        when(mcp.harness == 'claude', mcp.block('Bash', 'Monitor', 'TaskStop')),
        when(mcp.harness == 'claude', mcp.allow_commands('Monitor', 'watch it')),
        when(mcp.harness == 'claude', mcp.serve('TaskStop')),
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = WatchingBro()
    assert bro.blocked_tool_names('claude') == ('Bash',)
    assert bro.narrowed_tool_commands('claude') == {'Monitor': ('watch it',)}

  def test_serving_a_tool_the_bro_never_blocked_raises(self):
    class InvalidBro(BaseBro):
      name = 'invalid-serving'
      description = 'd'
      tools: ClassVar = [when(mcp.harness == 'claude', mcp.serve('TaskStop'))]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match='TaskStop is served whole but never blocked'):
      InvalidBro().blocked_tool_names('claude')

  def test_a_summoning_run_keeps_its_block_of_the_shell(self, monkeypatch):
    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))
    bro = _ShellBlockingBro()
    assert {'Bash', 'Monitor'} <= set(bro.blocked_tool_names('claude'))
    assert bro.narrowed_tool_commands('claude') == {}

  def test_a_summoning_run_keeps_its_declared_shell_narrowing(self, monkeypatch):
    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))

    class WatchingBro(BaseBro):
      name = 'watching-and-summoning'
      description = 'd'
      tools: ClassVar = [claude.block(*claude.SHELL), mcp.brash('watch it')]

      def __init__(self):
        super().__init__(system_prompt='')

    assert WatchingBro().narrowed_tool_commands('claude') == {
      'Bash': ('watch it',),
      'Monitor': ('watch it',),
    }

  def test_an_unwithheld_shell_is_left_as_it_is(self, monkeypatch):
    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))

    class OpenBro(BaseBro):
      name = 'open-shell'
      description = 'd'
      tools: ClassVar = [claude.block('Monitor')]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = OpenBro()
    assert bro.blocked_tool_names('claude') == ('Monitor',)
    assert bro.narrowed_tool_commands('claude') == {}


class _ShellBlockingBro(BaseBro):
  name = 'blocking-shell'
  description = 'd'
  tools: ClassVar = [claude.block(*claude.SHELL)]

  def __init__(self):
    super().__init__(system_prompt='')


class TestConditionalComponents:
  # a bro instance composes for the bro harness, so `when`-wrapped entries are
  # decided against `#harness = bro` at construction.
  def test_off_harness_server_excluded_and_never_built(self):
    def build():
      raise AssertionError('an unmatched spec must never build')

    class CondBro(BaseBro):
      name = 'cond'
      description = 'd'
      tools: ClassVar = [when(mcp.harness == 'claude', _server_layer(MCPServerSpec(build=build)))]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = CondBro()
    assert bro._mcp_specs == []
    assert bro._live_mcp_servers() == []

  def test_matching_condition_included(self):
    class MatchBro(BaseBro):
      name = 'match'
      description = 'd'
      tools: ClassVar = [when(mcp.harness == 'bro', _make_layer('a'))]

      def __init__(self):
        super().__init__(system_prompt='')

    assert len(MatchBro()._mcp_specs) == 1

  def test_bool_condition_is_a_constant(self):
    class BoolBro(BaseBro):
      name = 'bool'
      description = 'd'
      tools: ClassVar = [when(False, _make_layer('a')), _make_layer('b')]

      def __init__(self):
        super().__init__(system_prompt='')

    assert len(BoolBro()._mcp_specs) == 1

  def test_off_harness_data_source_excluded_everywhere(self):
    class CondSourceBro(BaseBro):
      name = 'cond-source'
      description = 'd'
      data_sources: ClassVar = [when(mcp.harness == 'claude', _SecretSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = CondSourceBro()
    assert bro._data_sources == []
    assert '## Data sources' not in bro.system_prompt
    assert bro.needed_secrets() == ()
    assert bro._live_mcp_servers() == []


class TestFeatures:
  @pytest.fixture(autouse=True)
  def _register_xkey(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.known_names', lambda: frozenset({'xkey'}))

  def _bro_class(self):
    class FeatureBro(BaseBro):
      name = 'feature-bro'
      description = 'd'
      features: ClassVar = {'x': mcp.creds.contains('xkey')}
      tools: ClassVar = [when(feature('x'), _server_layer(MCPServerSpec.of(_SecretServer)))]
      system_prompt = 'base text{{when #features contains x}} FEATURE TEXT{{end}}'

    return FeatureBro

  def test_gated_component_and_text_follow_the_gates(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'xkey')
    on = self._bro_class()()
    assert len(on._mcp_specs) == 1
    assert 'FEATURE TEXT' in on.system_prompt
    assert set(on.needed_secrets()) == {'alpha', 'beta'}

    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
    off = self._bro_class()()
    assert off._mcp_specs == []
    assert 'FEATURE TEXT' not in off.system_prompt
    assert off.needed_secrets() == ()

  def test_gate_on_an_unregistered_kind_fails_the_probe(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.known_names', lambda: frozenset())
    with pytest.raises(ConditionError, match="'xkey' outside the set universe in '#creds contains"):
      self._bro_class()()

  def test_derived_pins_parent_feature_on(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)

    class Pinned(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': True}

    child = Pinned()
    assert len(child._mcp_specs) == 1
    assert 'FEATURE TEXT' in child.system_prompt

  def test_derived_disables_parent_feature(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'xkey')

    class Disabled(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': False}

    child = Disabled()
    assert child._mcp_specs == []
    assert 'FEATURE TEXT' not in child.system_prompt

  def test_gate_credential_is_tiered_with_the_feature(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
    gated = self._bro_class()()
    assert 'xkey' in gated.optional_secrets()
    assert 'xkey' not in gated.needed_secrets()

    class Pinned(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': True}

    pinned = Pinned()
    assert 'xkey' in pinned.needed_secrets()
    assert 'xkey' not in pinned.optional_secrets()

    class Disabled(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': False}

    disabled = Disabled()
    assert 'xkey' not in disabled.needed_secrets()
    assert 'xkey' not in disabled.optional_secrets()

  def test_regating_a_feature_replaces_its_credential(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.known_names', lambda: frozenset({'xkey', 'ykey'}))
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)

    class Regated(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': mcp.creds.contains('ykey')}

    regated = Regated()
    assert 'ykey' in regated.optional_secrets()
    assert 'xkey' not in regated.optional_secrets()

  def test_reenabling_a_disabled_feature_fails_construction(self):
    class Disabled(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': False}

    class Reenabled(Disabled):
      name = 'feature-grandchild'
      features: ClassVar = {'x': True}

    with pytest.raises(ValueError, match="re-enables feature 'x'"):
      Reenabled()

  def test_redeclaring_a_disabled_feature_off_is_allowed(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)

    class Disabled(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': False}

    class StillDisabled(Disabled):
      name = 'feature-grandchild'
      features: ClassVar = {'x': False}

    assert StillDisabled()._mcp_specs == []

  def test_gate_may_reference_only_the_gate_vocabulary(self):
    class SurfaceGated(BaseBro):
      name = 'surface-gated'
      description = 'd'
      features: ClassVar = {'x': mcp.harness == 'bro'}
      tools: ClassVar = [when(feature('x'), _make_layer('a'))]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ConditionError, match='unknown variable #harness'):
      SurfaceGated()

  def test_has_feature_probes_live_and_reads_undeclared_as_off(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'xkey')
    bro = self._bro_class()()
    assert bro.has_feature('x') is True
    assert bro.has_feature('ghost') is False
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
    assert bro.has_feature('x') is False

  def test_undeclared_feature_name_raises(self):
    class NoFeature(BaseBro):
      name = 'no-feature'
      description = 'd'
      tools: ClassVar = [when(feature('ghost'), _make_layer('a'))]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ConditionError, match='ghost'):
      NoFeature()


class TestClaudePersonaServers:
  def _bro(self):
    class PersonaBro(BaseBro):
      name = 'persona'
      description = 'd'
      tools: ClassVar = [
        when(mcp.harness == 'bro', _server_layer(MCPServerSpec.of(_SecretServer))),
        _make_layer('a'),
      ]
      data_sources: ClassVar = [when(mcp.harness == 'bro', _SecretSource())]

      def __init__(self):
        super().__init__(system_prompt='')

    return PersonaBro()

  def test_serves_only_claude_harness_components(self):
    servers = self._bro().assemble(harness='claude', include_raise=False)
    assert [s.namespace for s in servers] == ['test', 'bro']

  def test_service_server_carries_banner_but_not_raise(self):
    names = asyncio.run(
      _collect_tool_names(self._bro().assemble(harness='claude', include_raise=False))
    )
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
      tools: ClassVar = [_server_layer(MCPServerSpec.of(_SecretServer))]
      data_sources: ClassVar = [_SecretSource()]
      extra_secrets = ('delta',)

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ManifestBro()
    # the llm key is NOT in needed_secrets() — surfaces that run the bro add it
    assert bro.needed_secrets() == ('alpha', 'beta', 'delta', 'gamma')
    assert bro.llm_spec.needed_secrets() == ('openai',)  # default openai

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


class TestCredentialDeclarations:
  def test_extra_secrets_rejects_an_instance_name(self):
    class InstanceBro(BaseBro):
      name = 'instance-extra'
      description = 'd'
      extra_secrets = ('github+reviewer',)

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=r'InstanceBro\.extra_secrets.*github\+reviewer') as error:
      InstanceBro()
    message = str(error.value)
    assert "declare the bare kind 'github'" in message
    assert "store's defaults" in message
    assert 'project or bro creds list' in message
    assert '--cred flag' in message

  def test_toolset_manifest_rejects_an_instance_name_even_when_gated_off(self):
    class InstanceToolset(mcp.Toolset[None]):
      secrets = ('github+reviewer',)

    toolset = InstanceToolset('instance-tools')

    class InstanceBro(BaseBro):
      name = 'instance-toolset'
      description = 'd'
      tools: ClassVar = [when(False, mcp.mount(toolset))]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(
      ValueError, match=r'InstanceBro\.tools\[0\].*needed_secrets.*github\+reviewer'
    ):
      InstanceBro()

  @pytest.mark.parametrize('manifest_name', ['needed_secrets', 'optional_secrets'])
  def test_mcp_server_manifest_rejects_an_instance_name(self, manifest_name):
    manifest = {manifest_name: ('github+reviewer',)}
    spec = MCPServerSpec(build=lambda: _make_server('probe'), **manifest)

    class InstanceBro(BaseBro):
      name = 'instance-server'
      description = 'd'
      tools: ClassVar = [_server_layer(spec)]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=rf'{manifest_name}.*github\+reviewer'):
      InstanceBro()

  @pytest.mark.parametrize('manifest_name', ['needed_secrets', 'optional_secrets'])
  def test_data_source_manifest_rejects_an_instance_name(self, manifest_name):
    class InstanceSource(_SecretSource):
      pass

    setattr(InstanceSource, manifest_name, ('github+reviewer',))

    class InstanceBro(BaseBro):
      name = 'instance-source'
      description = 'd'
      data_sources: ClassVar = [InstanceSource()]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(
      ValueError,
      match=rf'InstanceBro\.data_sources\[0\] InstanceSource\.{manifest_name}.*github\+reviewer',
    ):
      InstanceBro()

  def test_feature_gate_rejects_an_instance_name(self):
    class InstanceBro(BaseBro):
      name = 'instance-feature'
      description = 'd'
      features: ClassVar = {'review': mcp.creds.contains('github+reviewer')}

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=r"InstanceBro\.features\['review'\].*github\+reviewer"):
      InstanceBro()

  def test_component_gate_rejects_an_instance_name(self):
    class InstanceBro(BaseBro):
      name = 'instance-component-gate'
      description = 'd'
      tools: ClassVar = [when(mcp.creds.contains('github+reviewer'), _make_layer('probe'))]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=r'InstanceBro\.tools\[0\] condition.*github\+reviewer'):
      InstanceBro()


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


class TestMayLaunch:
  def test_defaults_to_empty(self):
    class Plain(BaseBro):
      name = 'plain'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    assert Plain()._may_launch == ()

  def test_mro_unioned(self):
    class Base(BaseBro):
      name = 'base'
      description = 'd'
      may_launch = ('webview',)

      def __init__(self):
        super().__init__(system_prompt='')

    class Derived(Base):
      name = 'derived'
      may_launch = ('benchmark',)

    assert Derived()._may_launch == ('webview', 'benchmark')

  @pytest.mark.parametrize(
    'worker_type',
    ['bro.party.unboxed', 'bro.bros.bro-dev', 'webview.vnc'],
  )
  def test_payload_shaped_worker_type_is_refused(self, worker_type):
    class Invalid(BaseBro):
      name = 'invalid'
      description = 'd'
      may_launch = (worker_type,)

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=r'Invalid\.may_launch.*invalid worker type'):
      Invalid()

  def test_bro_is_refused_because_the_framework_seeds_it(self):
    class Invalid(BaseBro):
      name = 'invalid'
      description = 'd'
      may_launch = ('bro',)

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=r'Invalid\.may_launch.*framework seeds'):
      Invalid()


class TestEmptyManifest:
  def test_empty_when_no_components_and_keyless_llm(self):

    class Bare(BaseBro):
      name = 'bare'
      description = 'd'
      llm_spec = llm_llms_echo.LLMSpec()

      def __init__(self):
        super().__init__(system_prompt='')

    assert Bare().needed_secrets() == ()


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
  def test_toolset_optional_secret_reaches_the_bro_manifest(self):
    class OptionalToolset(mcp.Toolset[None]):
      optional_secrets = ('gamma',)

    class OptionalBro(BaseBro):
      name = 'optional-toolset'
      description = 'd'
      tools: ClassVar = [mcp.mount(OptionalToolset('optional-tools'))]

      def __init__(self):
        super().__init__(system_prompt='')

    assert OptionalBro().optional_secrets() == ('gamma',)

  def test_unions_mcp_and_datasource_optional(self):
    class OptBro(BaseBro):
      name = 'opt'
      description = 'd'
      tools: ClassVar = [_server_layer(MCPServerSpec.of(_OptionalServer))]
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
      tools: ClassVar = [_server_layer(MCPServerSpec.of(_BothServer))]
      data_sources: ClassVar = [_OptShared()]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = BothBro()
    assert 'shared' in bro.needed_secrets()
    assert bro.optional_secrets() == ()

  def test_missing_secrets_ignores_the_optional_tier(self, monkeypatch):
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
      llm_spec = llm_llms_echo.LLMSpec()
      data_sources: ClassVar = [OptionalSource()]

    optional_bro = OptionalBro()
    assert optional_bro.optional_secrets() == ('gamma',)
    assert optional_bro.missing_secrets() == ()


class TestProvisioning:
  def test_defaults_to_no_steps(self):
    class Plain(BaseBro):
      name = 'plain'
      description = 'd'

      def __init__(self):
        super().__init__(system_prompt='')

    Plain().provision_workspace(Path('/workspace'))

  def test_steps_run_in_mro_order_against_the_workspace(self):
    applied: list[tuple[str, Path]] = []

    class Base(BaseBro):
      name = 'base'
      description = 'd'
      provisioning = (lambda workspace: applied.append(('base', workspace)),)

      def __init__(self):
        super().__init__(system_prompt='')

    class Derived(Base):
      name = 'derived'
      provisioning = (lambda workspace: applied.append(('derived', workspace)),)

    Derived().provision_workspace(Path('/workspace'))
    assert applied == [('base', Path('/workspace')), ('derived', Path('/workspace'))]


async def _collect_tool_names(servers):
  names: set[str] = set()
  for server in servers:
    for tool in await server.list_tools():
      names.add(tool.name)
  return names


async def _find_tool(
  bro: BaseBro,
  name: str,
  *,
  run: Optional[StubRun] = None,
  harness: mcp.HarnessLike = 'bro',
):
  for candidate in await _service_server(bro, run=run, harness=harness).list_tools():
    if candidate.name == name:
      return candidate
  raise AssertionError(f'no {name!r} tool on the service server')


async def _find_raise_tool(bro: BaseBro, harness: mcp.HarnessLike = 'bro'):
  for tool in await _service_server(bro, harness=harness).list_tools():
    if tool.name == 'raise':
      return tool
  raise AssertionError('raise tool not found on bro service server')


class EndingHarness(Harness):
  name = 'ending'

  def __init__(self, *, available: bool = True):
    super().__init__()
    self.available = available
    self.calls: list[tuple[str, SessionEndReason]] = []

  def can_end_session(self) -> bool:
    return self.available

  async def end_session(self, result: str, end_reason: SessionEndReason) -> str:
    self.calls.append((result, end_reason))
    return 'session ended'


class OwnToolsHarness(Harness):
  name = 'own-tools'

  def own_tools(self, bro, live_run):
    assert isinstance(bro, EchoBro)
    assert isinstance(live_run, StubRun)

    def harness_tool() -> str:
      return 'served by the harness'

    return (FunctionTool(harness_tool, description='a harness-owned tool'),)


class TestHarnessOwnTools:
  @pytest.mark.asyncio
  async def test_mounts_the_harness_roster_and_extends_the_tools_universe(self):
    server = _service_server(EchoBro(), harness=OwnToolsHarness())

    assert {tool.name for tool in await server.list_tools()} >= {'banner', 'raise', 'harness_tool'}
    assert server.tool_universe == (*bro_module._CORE_SERVICE_TOOL_NAMES, 'harness_tool')


class TestRaise:
  @pytest.mark.asyncio
  async def test_raise_tool_included_in_non_interactive_mode(self):
    bro = EchoBro()
    names = await _collect_tool_names(_native_servers(bro, hold='unattended'))
    assert 'raise' in names

  @pytest.mark.asyncio
  async def test_raise_tool_excluded_at_every_other_hold(self):
    bro = EchoBro()
    for hold in ('detached', 'attended', 'guided'):
      names = await _collect_tool_names(_native_servers(bro, hold=hold))
      assert 'raise' not in names

  @pytest.mark.asyncio
  async def test_raise_tool_ends_through_the_harness(self):
    harness = EndingHarness()
    tool = await _find_raise_tool(EchoBro(), harness)

    assert await tool.call({'reason': 'missing api key'}) == 'session ended'
    assert harness.calls == [('missing api key', 'raised')]

  @pytest.mark.asyncio
  async def test_raise_description_states_the_session_end_for_every_harness(self):
    for harness in (EndingHarness(), Harness('identity-only')):
      tool = await _find_raise_tool(EchoBro(), harness)
      assert 'ends the session' in tool.description
      assert '{{' not in tool.description


class TestAnswer:
  async def _names(self, harness: Harness) -> set[str]:
    server = bro_module._build_service_server(EchoBro(), include_raise=False, harness=harness)
    return {tool.name for tool in await server.list_tools()}

  @pytest.mark.asyncio
  async def test_mounted_exactly_when_the_harness_can_end_a_summoned_run(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setenv('RIDE_SUMMONED', '1')

    assert 'answer' in await self._names(EndingHarness())
    assert 'answer' not in await self._names(EndingHarness(available=False))

  @pytest.mark.asyncio
  async def test_unmounted_without_the_summoned_mark_or_channel(self, monkeypatch):
    harness = EndingHarness()
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    assert 'answer' not in await self._names(harness)
    monkeypatch.delenv('BROKER_CHANNEL')
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    assert 'answer' not in await self._names(harness)

  async def _tool(self, harness: EndingHarness, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    server = bro_module._build_service_server(EchoBro(), include_raise=False, harness=harness)
    for tool in await server.list_tools():
      if tool.name == 'answer':
        return tool
    raise AssertionError('answer tool not found on the service build')

  @pytest.mark.asyncio
  async def test_answer_ends_through_the_harness(self, monkeypatch):
    harness = EndingHarness()
    tool = await self._tool(harness, monkeypatch)

    assert await tool.call({'answer': 'the verdict'}) == 'session ended'
    assert harness.calls == [('the verdict', 'ok')]
    assert 'ends the session' in tool.description
    assert '{{' not in tool.description

  @pytest.mark.asyncio
  async def test_answer_rejects_oversize_output_before_ending(self, monkeypatch):
    harness = EndingHarness()
    tool = await self._tool(harness, monkeypatch)

    with pytest.raises(ValueError, match='answer too large; mint an artifact'):
      await tool.call({'answer': 'x' * ((64 << 10) + 1)})
    assert harness.calls == []


class TestSessionModePrompts:
  def test_non_interactive_runs_pin_the_unattended_hold(self):
    bro = EchoBro()
    prompt = bro.system_prompt_for(hold='unattended')
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
    prompt = bro.system_prompt_for(hold='guided')
    assert 'clarifying question' in prompt
    assert bro.system_prompt in prompt
    assert '# Guided session' in prompt
    assert '# Unattended session' not in prompt

  def test_native_system_prompt_passes_the_runs_talk_to_the_summoned_contract(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    monkeypatch.setenv(BROKER_TALK, 'worker.question')
    prompt = EchoBro().system_prompt_for(hold='unattended')
    assert 'call `bro::quest_ask` on `self`' in prompt
    assert 'quest does not permit' not in prompt


class TestBannerTool:
  @pytest.mark.asyncio
  async def test_present_on_both_service_builds(self):
    bro = EchoBro()
    non_interactive = await _collect_tool_names(_native_servers(bro, hold='unattended'))
    interactive = await _collect_tool_names(_native_servers(bro, hold='guided'))
    assert 'banner' in non_interactive
    assert 'banner' in interactive

  @pytest.mark.asyncio
  async def test_renders_the_llm_banner_with_the_bro_name_and_live_trail(self, monkeypatch):

    captured: dict = {}

    def fake_render_banner(llm=False, bro=None, trail_id=None):
      captured['llm'] = llm
      captured['bro'] = bro
      captured['trail_id'] = trail_id
      return 'isolation: boxed'

    monkeypatch.setattr(workspace_banner, 'render_banner', fake_render_banner)
    run = StubRun()
    tool = await _find_tool(EchoBro(), 'banner', run=run)
    assert await tool.call({}) == 'isolation: boxed'
    assert captured == {'llm': True, 'bro': 'echo', 'trail_id': None}
    # the run's trail opens after the tool is built, so it is read per call
    run.trail_id = '01trail'
    await tool.call({})
    assert captured['trail_id'] == '01trail'


_QUEST_TOOLS = {
  'summon',
  'quest_check',
  'quest_history',
  'quest_say',
  'quest_ask',
  'quest_share',
  'quest_list',
  'quest_cancel',
}


class TestSummonTool:
  @pytest.mark.asyncio
  async def test_absent_without_a_channel(self):
    # conftest drops BROKER_CHANNEL, so the plain construction has no channel
    bro = EchoBro()
    names = await _collect_tool_names([_service_server(bro)])
    assert _QUEST_TOOLS.isdisjoint(names)

  @pytest.mark.asyncio
  async def test_present_on_both_service_builds_when_a_channel_is_set(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    bro = EchoBro()
    non_interactive = await _collect_tool_names(_native_servers(bro, hold='unattended'))
    interactive = await _collect_tool_names(_native_servers(bro, hold='guided'))
    # interactive surfaces (`call`) summon too — only `raise` is non-interactive-only
    assert _QUEST_TOOLS <= set(non_interactive)
    assert _QUEST_TOOLS <= set(interactive)

  @pytest.mark.asyncio
  async def test_proxy_failure_state_keeps_the_broker_tools_present(self, monkeypatch):
    monkeypatch.setenv('BROKER_UPSTREAM', 'tcp://token@127.0.0.1:9')
    names = await _collect_tool_names([_service_server(EchoBro())])
    assert _QUEST_TOOLS <= set(names)

  @pytest.mark.asyncio
  async def test_quest_list_returns_the_journal_records(self, monkeypatch):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    listing = {'quests': [{'id': 'R1', 'state': 'ended', 'outcome': 'ok'}]}
    monkeypatch.setattr(quest_module, 'list_quests', lambda: listing)
    tool = await _find_tool(EchoBro(), 'quest_list')
    assert await tool.call({}) == listing

  @pytest.mark.asyncio
  async def test_service_tool_schemas_have_no_waiting_controls_on_either_harness(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')

    def properties(tool) -> set[str]:
      return set(tool.parameters['properties'])

    for harness in ('bro', 'claude'):
      tools = {
        tool.name: tool for tool in await _service_server(EchoBro(), harness=harness).list_tools()
      }
      assert 'detach' not in properties(tools['summon'])
      assert 'passes' in properties(tools['summon'])
      assert properties(tools['quest_check']) == {'quest_id'}
      assert properties(tools['quest_history']) == {'quest_id'}
      assert properties(tools['quest_say']) == {'quest_id', 'text', 'reply_to'}
      assert properties(tools['quest_ask']) == {'quest_id', 'text', 'reply_to'}
      assert properties(tools['quest_share']) == {'quest_id', 'ref'}
      assert properties(tools['quest_cancel']) == {'quest_id'}

  @pytest.mark.asyncio
  @pytest.mark.parametrize('harness', ['bro', 'claude'])
  async def test_summon_returns_after_acceptance_on_either_harness(self, monkeypatch, harness):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def accepted(target, prompt, **kwargs):
      calls.append((target, prompt, kwargs))
      return 'REQ-1'

    monkeypatch.setattr(summon_module, 'summon_detached', accepted)
    tool = await _find_tool(
      EchoBro(), 'summon', run=StubRun(tool_step={'step_id': 9, 'index': 2}), harness=harness
    )

    assert await tool.call({'target': 'dev', 'prompt': 'work', 'passes': ['github+work']}) == {
      'state': 'accepted',
      'quest_id': 'REQ-1',
    }
    assert calls[0][0:2] == ('dev', 'work')
    assert calls[0][2]['step_id'] == 9
    assert calls[0][2]['index'] == 2
    assert calls[0][2]['passes'] == ['github+work']

  @pytest.mark.asyncio
  @pytest.mark.parametrize('harness', ['bro', 'claude'])
  async def test_quest_ask_asks_without_waiting_on_either_harness(self, monkeypatch, harness):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def ask(quest_id, text, *, reply_to):
      calls.append((quest_id, text, reply_to))
      return quest_module.Asked(quest_id, 'QUESTION-1')

    monkeypatch.setattr(quest_module, 'ask', ask)
    tool = await _find_tool(EchoBro(), 'quest_ask', harness=harness)

    assert await tool.call({'quest_id': 'REQ-1', 'text': 'approve?'}) == {
      'state': 'asked',
      'quest_id': 'REQ-1',
      'question_id': 'QUESTION-1',
    }
    assert calls == [('REQ-1', 'approve?', None)]

  @pytest.mark.asyncio
  async def test_quest_say_returns_the_sent_state(self, monkeypatch):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def say(quest_id, text, *, reply_to):
      calls.append((quest_id, text, reply_to))
      return 'OWN-QUEST'

    monkeypatch.setattr(quest_module, 'say', say)
    for harness in ('bro', 'claude'):
      tool = await _find_tool(EchoBro(), 'quest_say', harness=harness)
      assert await tool.call({'quest_id': 'self', 'text': 'yes', 'reply_to': 'Q1'}) == {
        'state': 'sent',
        'quest_id': 'OWN-QUEST',
      }
    assert calls == [('self', 'yes', 'Q1')] * 2

  @pytest.mark.asyncio
  async def test_quest_share_hands_a_ref_to_a_live_child(self, monkeypatch):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def share(quest_id, ref):
      calls.append((quest_id, ref))
      return quest_id

    monkeypatch.setattr(quest_module, 'share', share)
    tool = await _find_tool(EchoBro(), 'quest_share')
    ref = f'sha256:{"a" * 64}'

    assert await tool.call({'quest_id': 'REQ-1', 'ref': ref}) == {
      'state': 'shared',
      'quest_id': 'REQ-1',
      'ref': ref,
    }
    assert calls == [('REQ-1', ref)]

  @pytest.mark.asyncio
  @pytest.mark.parametrize('harness', ['bro', 'claude'])
  async def test_quest_cancel_returns_after_acceptance_on_either_harness(
    self, monkeypatch, harness
  ):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setattr(
      quest_module,
      'request_cancel',
      lambda quest_id: quest_module.CancelStatus('accepted', quest_id),
    )
    tool = await _find_tool(EchoBro(), 'quest_cancel', harness=harness)

    assert await tool.call({'quest_id': 'REQ-1'}) == {
      'state': 'accepted',
      'quest_id': 'REQ-1',
    }

  @pytest.mark.asyncio
  @pytest.mark.parametrize('harness', ['bro', 'claude'])
  async def test_check_reports_running_completed_and_a_stalled_child(self, monkeypatch, harness):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    outcomes = [
      quest_module.Outcome('REQ-1', trail_id='T1'),
      quest_module.Outcome(
        'REQ-1', trail_id='T1', questions=(quest_module.Question('Q1', 'approve?', 'REQ-1'),)
      ),
      quest_module.Outcome('REQ-1', answer='pong', trail_id='T1'),
    ]
    calls = []

    def check(quest_id):
      calls.append(quest_id)
      return outcomes.pop(0)

    monkeypatch.setattr(quest_module, 'check', check)
    tool = await _find_tool(EchoBro(), 'quest_check', harness=harness)
    assert await tool.call({'quest_id': 'REQ-1'}) == {
      'state': 'running',
      'quest_id': 'REQ-1',
      'trail_id': 'T1',
    }
    assert await tool.call({'quest_id': 'REQ-1'}) == {
      'state': 'question',
      'quest_id': 'REQ-1',
      'trail_id': 'T1',
      'questions': [{'id': 'Q1', 'text': 'approve?'}],
    }
    assert await tool.call({'quest_id': 'REQ-1'}) == {
      'state': 'completed',
      'quest_id': 'REQ-1',
      'trail_id': 'T1',
      'answer': 'pong',
    }
    assert calls == ['REQ-1'] * 3

  @pytest.mark.asyncio
  @pytest.mark.parametrize('harness', ['bro', 'claude'])
  async def test_history_reports_the_marked_conversation(self, monkeypatch, harness):
    from bro import quest as quest_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    question = {'seq': 1, 'from': 'owner', 'id': 'Q1', 'head': {'text': 'go?'}, 'pending': True}

    def history(quest_id):
      assert quest_id == 'self'
      return quest_module.History(
        'OWN-QUEST',
        ('owner.question',),
        (question,),
        truncated=True,
        chat_seq=1,
        awaiting=(quest_module.Question('Q1', 'go?', 'OWN-QUEST'),),
      )

    monkeypatch.setattr(quest_module, 'history', history)
    tool = await _find_tool(EchoBro(), 'quest_history', harness=harness)
    assert await tool.call({'quest_id': 'self'}) == {
      'quest_id': 'OWN-QUEST',
      'talk': ['owner.question'],
      'messages': [question],
      'truncated': True,
    }


class TestPersona:
  def test_persona_honors_explicit_override(self):
    assert EchoBro().persona == '# Persona: echo\n\nyou echo'


class TestAgentIdentity:
  def test_agent_namespaces_the_bro_name(self):
    assert EchoBro().agent == 'bro//echo'


class TestShellRoster:
  def test_exact_rosters_fold_in_declaration_order(self):
    class ShellBro(BaseBro):
      name = 'shell-roster'
      description = 'd'
      tools: ClassVar = [mcp.brash('git status'), mcp.brash('git diff', 'git status')]

      def __init__(self):
        super().__init__(system_prompt='')

    selection = ShellBro()._selected_tools_for('bro')
    assert selection.brash_commands == ('git status', 'git diff')
    assert selection.brash_unrestricted is False

  def test_any_dominates_exact_commands(self):
    class ShellBro(BaseBro):
      name = 'shell-any'
      description = 'd'
      tools: ClassVar = [mcp.brash('git status'), mcp.brash(mcp.ANY)]

      def __init__(self):
        super().__init__(system_prompt='')

    selection = ShellBro()._selected_tools_for('bro')
    assert selection.brash_unrestricted is True

  def test_finite_claude_roster_gates_bash_and_monitor_and_returns_control(self):
    class ShellBro(BaseBro):
      name = 'shell-gated'
      description = 'd'
      tools: ClassVar = [claude.block(*claude.SHELL), mcp.brash('git status')]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ShellBro()
    assert bro.blocked_tool_names('claude') == ()
    assert bro.narrowed_tool_commands('claude') == {
      'Bash': ('git status',),
      'Monitor': ('git status',),
    }

  def test_finite_claude_roster_requires_the_shell_to_be_blocked(self):
    class InvalidBro(BaseBro):
      name = 'shell-unblocked'
      description = 'd'
      tools: ClassVar = [mcp.brash('git status')]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(
      ValueError, match='Bash is narrowed through the brash command list but never blocked'
    ):
      InvalidBro().blocked_tool_names('claude')

  def test_any_leaves_claude_native_shell_untouched(self):
    class ShellBro(BaseBro):
      name = 'shell-unrestricted'
      description = 'd'
      tools: ClassVar = [mcp.brash(mcp.ANY)]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ShellBro()
    assert bro.blocked_tool_names('claude') == ()
    assert bro.narrowed_tool_commands('claude') == {}

  def test_summoning_does_not_add_an_undeclared_shell_command(self, monkeypatch):
    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))
    selection = EchoBro()._selected_tools_for('bro')
    assert selection.brash_commands == ()
    assert selection.brash_unrestricted is False


class TestWatchServiceTools:
  @pytest.mark.asyncio
  async def test_watch_tools_apply_the_exact_brash_list_on_every_harness(self):
    class ExactBrashBro(BaseBro):
      name = 'exact-watch-brash'
      description = 'd'
      tools: ClassVar = [mcp.brash('printf allowed')]

      def __init__(self):
        super().__init__(system_prompt='')

    class ExactClaudeBrashBro(ExactBrashBro):
      name = 'exact-claude-watch-brash'
      tools: ClassVar = [claude.block(*claude.SHELL)]

    with watches.Owner.temporary() as owner:
      run = StubRun()
      run.watch_store = owner.store
      for harness, declaration in (
        (Harness('alternate'), ExactBrashBro()),
        ('claude', ExactClaudeBrashBro()),
      ):
        server = _service_server(declaration, run=run, harness=harness)
        with contextlib.closing(server):
          tools = {tool.name: tool for tool in await server.list_tools()}
          assert await tools['watch'].call({'command': ' printf allowed '}) == (
            'watching `printf allowed`'
          )
          with pytest.raises(ValueError, match='match one declared entry exactly'):
            await tools['watch'].call({'command': 'printf allowed; true'})
          assert await tools['unwatch'].call({'command': 'printf allowed'}) == (
            'stopped watching `printf allowed`'
          )
          owner.store.start('sleep 60')
          assert await tools['unwatch'].call({'command': ' sleep 60 '}) == (
            'stopped watching `sleep 60`'
          )

  @pytest.mark.asyncio
  async def test_unwatch_refuses_the_runtime_owned_session_watch(self):
    class SessionBrashBro(BaseBro):
      name = 'session-watch-brash'
      description = 'd'
      tools: ClassVar = [mcp.brash(watches.SESSION_WATCH_COMMAND)]

      def __init__(self):
        super().__init__(system_prompt='')

    with watches.Owner.temporary() as owner:
      run = StubRun()
      run.watch_store = owner.store
      server = _service_server(SessionBrashBro(), run=run, harness=Harness('alternate'))
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        with pytest.raises(watches.WatchError, match='owned by the runtime'):
          await tools['unwatch'].call({'command': watches.SESSION_WATCH_COMMAND})
