import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

import bro.bro as bro_module
import bro.llm.llms.echo as llm_llms_echo
import bro.mcp as mcp
import bro.workspace.banner as workspace_banner
from bro.base import credentials
from bro.base.condition import ConditionError, iff, when
from bro.base.liveness_test_helper import Liveness
from bro.bro import BaseBro, BroRaised, feature
from bro.broker.brotocol import TALK_ENV
from bro.datasources.file import FileSource
from bro.datasources.man import ManPage, ManSource
from bro.datasources.searchable import Hit, SearchableDataSource
from bro.harness import claude
from bro.inbox import Inbox
from bro.jobs import Job, Registry
from bro.llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, Tool
from bro.llm.tracker import ToolStepSource
from bro.mcp import MCPServerSpec, describe
from bro.summon import MAY_SUMMON_ENV, SUMMONED_ENV, encode_may_summon


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
    self.inbox = Inbox()
    self.registry = Registry(self.inbox)


def _native_servers(
  bro: BaseBro, *, hold: str = 'unattended', run: Optional[StubRun] = None
) -> list[MCPServer]:
  return bro.assemble(
    harness='bro',
    wire='bare',
    include_raise=hold == 'unattended',
    live_run=run if run is not None else StubRun(),
  )


def _service_server(
  bro: BaseBro, *, run: Optional[StubRun] = None, wire: mcp.Wire = 'bare'
) -> MCPServer:
  return bro_module._build_service_server(
    bro,
    include_raise=True,
    harness='bro',
    wire=wire,
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
    assert '`mcp__namespace__tool`' in bro.claude_system_prompt

  def test_claude_flavor_teaches_mcp_wire_form(self):
    class ToolBro(BaseBro):
      name = 'tooled'
      description = 'd'
      tools: ClassVar = [_make_layer('a')]

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

  def test_a_summoning_run_reaches_the_summon_watch_over_a_block_of_monitor(self, monkeypatch):
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    bro = _ShellBlockingBro()
    assert bro.narrowed_tool_commands('claude') == {'Monitor': (bro_module.SUMMON_WATCH_COMMAND,)}
    blocked = set(bro.blocked_tool_names('claude'))
    assert 'Bash' in blocked
    assert blocked.isdisjoint({'Monitor', 'TaskOutput', 'TaskStop'})

  def test_a_summoning_run_gains_the_summon_watch_on_a_narrowed_monitor(self, monkeypatch):
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))

    class WatchingBro(BaseBro):
      name = 'watching-and-summoning'
      description = 'd'
      tools: ClassVar = [claude.block(*claude.SHELL), mcp.shell('watch it')]

      def __init__(self):
        super().__init__(system_prompt='')

    assert WatchingBro().narrowed_tool_commands('claude') == {
      'Bash': ('watch it',),
      'Monitor': ('watch it', bro_module.SUMMON_WATCH_COMMAND),
    }

  def test_a_run_that_may_summon_nobody_keeps_its_block_of_monitor(self, monkeypatch):
    monkeypatch.delenv(MAY_SUMMON_ENV, raising=False)
    bro = _ShellBlockingBro()
    assert 'Monitor' in bro.blocked_tool_names('claude')
    assert bro.narrowed_tool_commands('claude') == {}

  def test_a_summoned_run_with_a_speaking_summoner_reaches_the_watch(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    monkeypatch.setenv(TALK_ENV, 'requester.say,worker.say')
    bro = _ShellBlockingBro()
    assert bro.narrowed_tool_commands('claude') == {'Monitor': (bro_module.SUMMON_WATCH_COMMAND,)}
    assert set(bro.blocked_tool_names('claude')).isdisjoint({'Monitor', 'TaskOutput', 'TaskStop'})

  def test_a_summoned_run_with_a_silent_summoner_keeps_monitor_blocked(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    monkeypatch.setenv(TALK_ENV, 'worker.say,worker.question')
    bro = _ShellBlockingBro()
    assert 'Monitor' in bro.blocked_tool_names('claude')
    assert bro.narrowed_tool_commands('claude') == {}

  def test_an_unwithheld_monitor_is_left_as_it_is(self, monkeypatch):
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))

    class OpenBro(BaseBro):
      name = 'open-monitor'
      description = 'd'
      tools: ClassVar = [claude.block('Bash')]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = OpenBro()
    assert bro.blocked_tool_names('claude') == ('Bash',)
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
    servers = self._bro().assemble(harness='claude', wire='mcp', include_raise=False)
    assert [s.namespace for s in servers] == ['test', 'bro']

  def test_service_server_carries_banner_but_not_raise(self):
    names = asyncio.run(
      _collect_tool_names(self._bro().assemble(harness='claude', wire='mcp', include_raise=False))
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
    assert "declare the bare kind 'github'" in str(error.value)
    assert '~/.bro.json or a --grant flag' in str(error.value)

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
  wire: mcp.Wire = 'bare',
):
  for candidate in await _service_server(bro, run=run, wire=wire).list_tools():
    if candidate.name == name:
      return candidate
  raise AssertionError(f'no {name!r} tool on the service server')


async def _find_raise_tool(bro: BaseBro):
  for tool in await _service_server(bro).list_tools():
    if tool.name == 'raise':
      return tool
  raise AssertionError('raise tool not found on bro service server')


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
  async def test_raise_tool_raises_bro_raised(self):
    bro = EchoBro()
    tool = await _find_raise_tool(bro)
    with pytest.raises(BroRaised) as exception:
      await tool.call({'reason': 'missing api key'})
    assert exception.value.reason == 'missing api key'


class TestMCPRaise:
  """the MCP flavor records the abort and terminates its managed runner."""

  async def _mcp_raise_tool(self):
    server = bro_module._build_service_server(
      EchoBro(), include_raise=True, harness='bro', wire='mcp'
    )
    for tool in await server.list_tools():
      if tool.name == 'raise':
        return tool
    raise AssertionError('raise tool not found on the mcp service build')

  @pytest.mark.asyncio
  async def test_mcp_raise_records_channel_and_kills_the_runner(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    channel = MagicMock()
    monkeypatch.setattr('bro.bro.RunLifecycle.from_env', lambda: channel)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._mcp_raise_tool()
    await tool.call({'reason': 'missing api key'})
    channel.completed.assert_called_once_with('missing api key', 'raised')
    channel.close.assert_called_once_with()
    assert kills == [(4242, signal.SIGTERM)]

  @pytest.mark.asyncio
  async def test_mcp_raise_kills_without_a_channel(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    monkeypatch.setattr('bro.bro.RunLifecycle.from_env', lambda: None)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._mcp_raise_tool()
    await tool.call({'reason': 'no tool fits'})
    assert kills == [(4242, signal.SIGTERM)]

  @pytest.mark.asyncio
  async def test_mcp_raise_kills_even_when_the_channel_emission_fails(self, monkeypatch, tmp_path):
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    channel = MagicMock()
    channel.completed.side_effect = ConnectionError('channel closed')
    monkeypatch.setattr('bro.bro.RunLifecycle.from_env', lambda: channel)
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


class TestAnswer:
  """the summoned run's delivery tool: mounted only where a summoned child can
  actually send the terminal, ending the run by exception (bare) or by channel
  emission + session termination (mcp)."""

  async def _names(self, wire) -> set[str]:
    server = bro_module._build_service_server(
      EchoBro(), include_raise=False, harness='bro', wire=wire
    )
    return {tool.name for tool in await server.list_tools()}

  @pytest.mark.asyncio
  async def test_mounted_for_a_summoned_run_with_a_channel(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    assert 'answer' in await self._names('bare')

  @pytest.mark.asyncio
  async def test_unmounted_without_the_summoned_mark_or_channel(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    assert 'answer' not in await self._names('bare')
    monkeypatch.delenv('BROKER_CHANNEL')
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    assert 'answer' not in await self._names('bare')

  @pytest.mark.asyncio
  async def test_mcp_flavor_needs_a_killable_session(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    assert 'answer' not in await self._names('mcp')
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    assert 'answer' in await self._names('mcp')

  async def _tool(self, wire, monkeypatch, tmp_path):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setenv('RIDE_SUMMONED', '1')
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    server = bro_module._build_service_server(
      EchoBro(), include_raise=False, harness='bro', wire=wire
    )
    for tool in await server.list_tools():
      if tool.name == 'answer':
        return tool
    raise AssertionError('answer tool not found on the service build')

  @pytest.mark.asyncio
  async def test_bare_answer_ends_the_run_with_the_answer(self, monkeypatch, tmp_path):
    tool = await self._tool('bare', monkeypatch, tmp_path)
    with pytest.raises(bro_module.AnswerDelivered) as exception:
      await tool.call({'answer': 'the verdict'})
    assert exception.value.answer == 'the verdict'

  @pytest.mark.asyncio
  async def test_bare_answer_rejects_oversize_output_before_ending(self, monkeypatch, tmp_path):
    tool = await self._tool('bare', monkeypatch, tmp_path)
    with pytest.raises(ValueError, match='answer too large; mint an artifact'):
      await tool.call({'answer': 'x' * ((64 << 10) + 1)})

  @pytest.mark.asyncio
  async def test_mcp_answer_records_the_terminal_and_kills_the_runner(self, monkeypatch, tmp_path):
    channel = MagicMock()
    monkeypatch.setattr('bro.bro.RunLifecycle.from_env', lambda: channel)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._tool('mcp', monkeypatch, tmp_path)
    await tool.call({'answer': 'the verdict'})
    channel.completed.assert_called_once_with('the verdict', 'ok')
    channel.close.assert_called_once_with()
    assert kills == [(4242, signal.SIGTERM)]

  @pytest.mark.asyncio
  async def test_mcp_answer_rejects_oversize_output_without_killing(self, monkeypatch, tmp_path):
    channel = MagicMock()
    monkeypatch.setattr('bro.bro.RunLifecycle.from_env', lambda: channel)
    kills = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._tool('mcp', monkeypatch, tmp_path)
    with pytest.raises(ValueError, match='answer too large; mint an artifact'):
      await tool.call({'answer': 'x' * ((64 << 10) + 1)})
    assert kills == []
    channel.completed.assert_not_called()

  @pytest.mark.asyncio
  async def test_mcp_answer_without_a_channel_spares_the_session(self, monkeypatch, tmp_path):
    # unlike raise, an undeliverable answer must not kill the session — the
    # summoner would never hear it; the agent gets the error instead
    monkeypatch.setattr('bro.bro.RunLifecycle.from_env', lambda: None)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    tool = await self._tool('mcp', monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='cannot reach the summoner'):
      await tool.call({'answer': 'the verdict'})
    assert kills == []


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
    monkeypatch.setenv(TALK_ENV, 'worker.question')
    prompt = EchoBro().system_prompt_for(hold='unattended')
    assert '`question=true`' in prompt
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


class _FakeSummonClient:
  """stands in for summon.open_client(): records the close the tool owes it."""

  def __init__(self):
    self.closed = False

  def close(self, confirm: bool = False) -> None:
    del confirm
    self.closed = True

  def __enter__(self):
    return self

  def __exit__(self, *_exception_info):
    self.close()


class TestSummonTool:
  @pytest.mark.asyncio
  async def test_absent_without_a_channel(self):
    # conftest drops BROKER_CHANNEL, so the plain construction has no channel
    bro = EchoBro()
    names = await _collect_tool_names([_service_server(bro)])
    assert 'summon' not in names
    assert 'summon_say' not in names
    assert 'summon_check' not in names

  @pytest.mark.asyncio
  async def test_present_on_both_service_builds_when_a_channel_is_set(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    bro = EchoBro()
    non_interactive = await _collect_tool_names(_native_servers(bro, hold='unattended'))
    interactive = await _collect_tool_names(_native_servers(bro, hold='guided'))
    # interactive surfaces (`call`) summon too — only `raise` is non-interactive-only
    assert {'summon', 'summon_say', 'summon_check', 'summon_list', 'summon_cancel'} <= set(
      non_interactive
    )
    assert {'summon', 'summon_say', 'summon_check', 'summon_list', 'summon_cancel'} <= set(
      interactive
    )

  @pytest.mark.asyncio
  async def test_proxy_failure_state_keeps_the_broker_tools_present(self, monkeypatch):
    monkeypatch.setenv('BROKER_UPSTREAM', 'tcp://token@127.0.0.1:9')
    names = await _collect_tool_names([_service_server(EchoBro())])
    assert {'summon', 'summon_say', 'summon_check', 'summon_list', 'summon_cancel'} <= set(names)

  @pytest.mark.asyncio
  async def test_summon_list_returns_the_journal_records(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    listing = {'quests': [{'id': 'R1', 'state': 'ended', 'outcome': 'ok'}]}
    monkeypatch.setattr(summon_module, 'list_summons', lambda: listing)
    tool = await _find_tool(EchoBro(), 'summon_list')
    assert await tool.call({}) == listing

  @pytest.mark.asyncio
  async def test_bare_service_tool_schemas_have_no_waiting_controls(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    bare = {tool.name: tool for tool in await _service_server(EchoBro()).list_tools()}
    served = {tool.name: tool for tool in await _service_server(EchoBro(), wire='mcp').list_tools()}

    assert 'detach' not in bare['summon'].parameters['properties']
    assert set(bare['summon_check'].parameters['properties']) == {'request_id'}
    assert set(bare['summon_say'].parameters['properties']) == {
      'text',
      'request_id',
      'reply_to',
      'question',
    }
    assert set(bare['summon_cancel'].parameters['properties']) == {'request_id'}
    assert 'detach' in served['summon'].parameters['properties']
    assert {'wait', 'timeout'} <= set(served['summon_check'].parameters['properties'])
    assert 'wait' in served['summon_say'].parameters['properties']
    assert 'timeout' in served['summon_cancel'].parameters['properties']

  @pytest.mark.asyncio
  async def test_bare_summon_returns_after_acceptance(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def accepted(target, prompt, **kwargs):
      calls.append((target, prompt, kwargs))
      return 'REQ-1'

    monkeypatch.setattr(summon_module, 'summon_detached', accepted)
    tool = await _find_tool(EchoBro(), 'summon', run=StubRun(tool_step={'step_id': 9, 'index': 2}))

    assert await tool.call({'target': 'dev', 'prompt': 'work'}) == {
      'state': 'accepted',
      'request_id': 'REQ-1',
    }
    assert calls[0][0:2] == ('dev', 'work')
    assert calls[0][2]['step_id'] == 9
    assert calls[0][2]['index'] == 2

  @pytest.mark.asyncio
  async def test_bare_summon_say_asks_without_waiting(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def say(text, request_id, *, reply_to, question):
      calls.append((text, request_id, reply_to, question))
      return summon_module.SayStatus('question', request_id, question_id='QUESTION-1')

    monkeypatch.setattr(summon_module, 'say', say)
    tool = await _find_tool(EchoBro(), 'summon_say')

    assert await tool.call({'text': 'approve?', 'request_id': 'REQ-1', 'question': True}) == {
      'state': 'question',
      'request_id': 'REQ-1',
      'id': 'QUESTION-1',
    }
    assert calls == [('approve?', 'REQ-1', None, True)]

  @pytest.mark.asyncio
  async def test_bare_summon_cancel_returns_after_acceptance(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setattr(
      summon_module,
      'request_cancel',
      lambda request_id: summon_module.CancelStatus('accepted', request_id),
    )
    tool = await _find_tool(EchoBro(), 'summon_cancel')

    assert await tool.call({'request_id': 'REQ-1'}) == {
      'state': 'accepted',
      'request_id': 'REQ-1',
    }

  @pytest.mark.asyncio
  async def test_mcp_summon_say_returns_structured_question_and_closes_its_client(
    self, monkeypatch
  ):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    client = _FakeSummonClient()
    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(
      summon_module,
      'say',
      lambda text, request_id, *, reply_to, wait, client: summon_module.SayStatus(
        'question', request_id, question_id='QUESTION-1'
      ),
    )
    tool = await _find_tool(EchoBro(), 'summon_say', wire='mcp')

    assert await tool.call({'text': 'approve?', 'request_id': 'REQ-1', 'wait': 60}) == {
      'state': 'question',
      'request_id': 'REQ-1',
      'id': 'QUESTION-1',
    }
    assert client.closed

  @pytest.mark.asyncio
  async def test_mcp_calls_summon_and_wait_off_loop(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls: list = []
    client = _FakeSummonClient()

    def fake_summon_and_wait(
      target,
      prompt,
      *,
      timeout=None,
      into=None,
      hold=None,
      grant=None,
      revoke=None,
      share=None,
      llm=None,
      harness=None,
      party=None,
      isolation=None,
      talk=None,
      step_id=None,
      index=None,
      on_sent=None,
      client=None,
      silence_timeout=None,
    ):
      calls.append(
        {
          'target': target,
          'prompt': prompt,
          'timeout': timeout,
          'into': into,
          'grant': grant,
          'revoke': revoke,
          'llm': llm,
          'party': party,
          'isolation': isolation,
          'talk': talk,
          'step_id': step_id,
          'index': index,
          'client': client,
          'silence_timeout': silence_timeout,
        }
      )
      assert on_sent is not None
      on_sent('REQ-ID')
      return 'the answer'

    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'summon_and_wait', fake_summon_and_wait)
    run = StubRun(tool_step={'step_id': 42, 'index': 3})
    tool = None
    for candidate in await _service_server(EchoBro(), run=run, wire='mcp').list_tools():
      if candidate.name == 'summon':
        tool = candidate
    assert tool is not None
    result = await tool.call(
      {
        'target': 'dev',
        'prompt': 'deploy',
        'timeout': 60,
        'grant': ['aws', '@bro'],
        'revoke': ['openai'],
        'llm': 'openai:sol:high+fast',
        'party': 'start',
        'isolation': 'unboxed',
        'talk': ['worker.question'],
      }
    )
    assert result == {'state': 'completed', 'request_id': 'REQ-ID', 'answer': 'the answer'}
    # the request carries the summon call's own tool_call step for provenance
    assert calls == [
      {
        'target': 'dev',
        'prompt': 'deploy',
        'timeout': 60,
        'into': None,
        'grant': ['aws', '@bro'],
        'revoke': ['openai'],
        'llm': 'openai:sol:high+fast',
        'party': 'start',
        'isolation': 'unboxed',
        'talk': ['worker.question'],
        'step_id': 42,
        'index': 3,
        'client': client,
        'silence_timeout': summon_module.READ_WAIT_SECONDS,
      }
    ]
    assert client.closed  # the per-call client is closed on the way out

  @pytest.mark.asyncio
  async def test_mcp_blocking_summon_returns_a_structured_child_question(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setattr(summon_module, 'open_client', _FakeSummonClient)

    def ask(*args, on_sent=None, **kwargs):
      assert on_sent is not None
      on_sent('REQ-1')
      return summon_module.SummonQuestion('QUESTION-1', 'approve?')

    monkeypatch.setattr(summon_module, 'summon_and_wait', ask)
    tool = await _find_tool(EchoBro(), 'summon', wire='mcp')

    assert await tool.call({'target': 'dev', 'prompt': 'work', 'talk': ['worker.question']}) == {
      'state': 'question',
      'request_id': 'REQ-1',
      'question': {'id': 'QUESTION-1', 'text': 'approve?'},
    }

  @pytest.mark.asyncio
  async def test_mcp_detach_returns_the_request_id_without_waiting(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls: list = []

    def fake_summon_detached(
      target,
      prompt,
      *,
      timeout=None,
      into=None,
      hold=None,
      grant=None,
      revoke=None,
      share=None,
      llm=None,
      harness=None,
      party=None,
      isolation=None,
      talk=None,
      step_id=None,
      index=None,
    ):
      calls.append({'target': target, 'prompt': prompt, 'timeout': timeout, 'into': into})
      return 'REQ-ID'

    def fail_summon_and_wait(*args, **kwargs):
      raise AssertionError('detach must not block on summon_and_wait')

    monkeypatch.setattr(summon_module, 'summon_detached', fake_summon_detached)
    monkeypatch.setattr(summon_module, 'summon_and_wait', fail_summon_and_wait)
    tool = await _find_tool(EchoBro(), 'summon', wire='mcp')
    result = await tool.call({'target': 'dev', 'prompt': 'deploy', 'detach': True})
    assert result == {'state': 'accepted', 'request_id': 'REQ-ID'}
    assert calls == [{'target': 'dev', 'prompt': 'deploy', 'timeout': None, 'into': None}]

  @pytest.mark.asyncio
  async def test_check_reports_pending_and_completed(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    statuses = [
      summon_module.SummonStatus(pending=True, trail_id='T1'),
      summon_module.SummonStatus(pending=False, answer='pong', trail_id='T1'),
    ]
    monkeypatch.setattr(summon_module, 'check_summon', lambda request_id: statuses.pop(0))
    tool = await _find_tool(EchoBro(), 'summon_check')
    assert await tool.call({'request_id': 'REQ-1'}) == {
      'state': 'pending',
      'request_id': None,
      'talk': [],
      'pending': [],
      'messages': [],
      'trail_id': 'T1',
    }
    assert await tool.call({'request_id': 'REQ-1'}) == {
      'state': 'completed',
      'request_id': None,
      'talk': [],
      'pending': [],
      'messages': [],
      'trail_id': 'T1',
      'answer': 'pong',
    }

  @pytest.mark.asyncio
  async def test_check_without_a_request_id_reads_the_own_quest(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls = []

    def check(request_id=None):
      calls.append(request_id)
      return summon_module.SummonStatus(pending=True, request_id='OWN-QUEST')

    monkeypatch.setattr(summon_module, 'check_summon', check)
    tool = await _find_tool(EchoBro(), 'summon_check')

    result = await tool.call({})
    assert isinstance(result, dict)
    assert result['request_id'] == 'OWN-QUEST'
    assert calls == [None]

  @pytest.mark.asyncio
  async def test_check_schema_has_no_conversation_cursor(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    tool = await _find_tool(EchoBro(), 'summon_check')
    assert 'last_seen' not in tool.parameters['properties']

  @pytest.mark.asyncio
  async def test_cancelled_mcp_summon_closes_its_client(self, monkeypatch):
    # the client-side abort path: cancelling the tool call (the MCP client timed
    # out or aborted) must close the per-call channel client, which unblocks the
    # worker thread and detaches the broxy route
    import threading

    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    client = _FakeSummonClient()
    entered = threading.Event()
    release = threading.Event()

    def fake_summon_and_wait(
      target,
      prompt,
      *,
      timeout=None,
      into=None,
      hold=None,
      grant=None,
      revoke=None,
      share=None,
      llm=None,
      harness=None,
      party=None,
      isolation=None,
      talk=None,
      step_id=None,
      index=None,
      on_sent=None,
      client=None,
      silence_timeout=None,
    ):
      entered.set()
      release.wait()
      raise summon_module.SummonError('broker channel closed awaiting the summon result')

    def fake_close(confirm: bool = False) -> None:
      del confirm
      client.closed = True
      release.set()

    monkeypatch.setattr(client, 'close', fake_close)
    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'summon_and_wait', fake_summon_and_wait)
    tool = await _find_tool(EchoBro(), 'summon', wire='mcp')
    task = asyncio.create_task(tool.call({'target': 'dev', 'prompt': 'deploy'}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert client.closed

  @pytest.mark.asyncio
  async def test_transport_caution_only_on_the_mcp_wire(self, monkeypatch):
    # wire 'mcp' builds are consumed over an MCP transport with a client-side
    # call budget; their summon descriptions carry the timeout caution
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    bro_instance = EchoBro()
    mcp_build = bro_module._build_service_server(
      bro_instance, include_raise=False, harness='bro', wire='mcp'
    )
    bare_build = bro_module._build_service_server(
      bro_instance, include_raise=False, harness='bro', wire='bare'
    )
    mcp_tools = {t.name: t for t in await mcp_build.list_tools()}
    bare_tools = {t.name: t for t in await bare_build.list_tools()}
    for name in ('summon', 'summon_say', 'summon_check', 'summon_cancel'):
      assert 'CAUTION' in mcp_tools[name].description
      assert 'CAUTION' not in bare_tools[name].description

  @pytest.mark.asyncio
  async def test_check_wait_collects(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    calls: list = []
    client = _FakeSummonClient()

    def fake_wait_summon(request_id, *, timeout=None, client=None, wait_seconds=None):
      calls.append({'request_id': request_id, 'timeout': timeout, 'client': client})
      return summon_module.SummonStatus(pending=False, answer='collected')

    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'wait_summon', fake_wait_summon)
    tool = await _find_tool(EchoBro(), 'summon_check', wire='mcp')
    result = await tool.call({'request_id': 'REQ-1', 'wait': True, 'timeout': 60})
    assert result == {
      'state': 'completed',
      'request_id': None,
      'talk': [],
      'pending': [],
      'messages': [],
      'answer': 'collected',
    }
    assert calls == [{'request_id': 'REQ-1', 'timeout': 60, 'client': client}]
    assert client.closed

  @pytest.mark.asyncio
  async def test_check_wait_returns_pending_at_its_deadline(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    client = _FakeSummonClient()

    def fake_wait_summon(request_id, *, timeout=None, client=None):
      assert request_id == 'REQ-1'
      assert timeout == 60
      return summon_module.SummonStatus(pending=True, trail_id='T1')

    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'wait_summon', fake_wait_summon)
    tool = await _find_tool(EchoBro(), 'summon_check', wire='mcp')

    assert await tool.call({'request_id': 'REQ-1', 'wait': True, 'timeout': 60}) == {
      'state': 'pending',
      'request_id': None,
      'talk': [],
      'pending': [],
      'messages': [],
      'trail_id': 'T1',
    }
    assert client.closed

  @pytest.mark.asyncio
  async def test_cancel_waits_on_its_own_client_and_returns_the_ended_state(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    client = _FakeSummonClient()
    calls = []

    def fake_cancel_summon(request_id, *, timeout=None, client=None):
      calls.append({'request_id': request_id, 'timeout': timeout, 'client': client})
      return summon_module.CancelStatus(
        'ended', request_id, outcome='failed', reason='cancelled', trail_id='T1'
      )

    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(summon_module, 'cancel_summon', fake_cancel_summon)
    tool = await _find_tool(EchoBro(), 'summon_cancel', wire='mcp')

    assert await tool.call({'request_id': 'REQ-1', 'timeout': 60}) == {
      'state': 'ended',
      'request_id': 'REQ-1',
      'outcome': 'failed',
      'reason': 'cancelled',
      'trail_id': 'T1',
    }
    assert calls == [{'request_id': 'REQ-1', 'timeout': 60, 'client': client}]
    assert client.closed

  @pytest.mark.asyncio
  async def test_cancel_returns_pending_at_its_deadline(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    client = _FakeSummonClient()
    monkeypatch.setattr(summon_module, 'open_client', lambda: client)
    monkeypatch.setattr(
      summon_module,
      'cancel_summon',
      lambda request_id, *, timeout=None, client=None: summon_module.CancelStatus(
        'pending', request_id
      ),
    )
    tool = await _find_tool(EchoBro(), 'summon_cancel', wire='mcp')

    assert await tool.call({'request_id': 'REQ-1', 'timeout': 5}) == {
      'state': 'pending',
      'request_id': 'REQ-1',
    }
    assert client.closed

  @pytest.mark.asyncio
  @pytest.mark.parametrize('timeout', [float('nan'), float('inf'), 0])
  async def test_mcp_cancel_rejects_a_non_finite_deadline(self, monkeypatch, timeout):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    tool = await _find_tool(EchoBro(), 'summon_cancel', wire='mcp')
    with pytest.raises(ValueError, match='finite positive'):
      await tool.call({'request_id': 'REQ-1', 'timeout': timeout})

  @pytest.mark.asyncio
  async def test_mcp_check_timeout_without_wait_is_an_error(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    tool = await _find_tool(EchoBro(), 'summon_check', wire='mcp')
    with pytest.raises(ValueError, match='wait'):
      await tool.call({'request_id': 'REQ-1', 'timeout': 60})

  @pytest.mark.asyncio
  @pytest.mark.parametrize('timeout', [float('nan'), float('inf')])
  async def test_mcp_check_wait_rejects_non_finite_deadlines(self, monkeypatch, timeout):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    tool = await _find_tool(EchoBro(), 'summon_check', wire='mcp')
    with pytest.raises(ValueError, match='finite positive'):
      await tool.call({'request_id': 'REQ-1', 'wait': True, 'timeout': timeout})

  @pytest.mark.asyncio
  async def test_mcp_summon_failure_propagates_as_the_tool_error(self, monkeypatch):
    from bro import summon as summon_module

    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')

    def fake_summon_and_wait(
      target,
      prompt,
      *,
      timeout=None,
      into=None,
      hold=None,
      grant=None,
      revoke=None,
      share=None,
      llm=None,
      harness=None,
      party=None,
      isolation=None,
      talk=None,
      step_id=None,
      index=None,
      on_sent=None,
      client=None,
      silence_timeout=None,
    ):
      raise summon_module.SummonError('summon denied: no')

    monkeypatch.setattr(summon_module, 'open_client', lambda: _FakeSummonClient())
    monkeypatch.setattr(summon_module, 'summon_and_wait', fake_summon_and_wait)
    bro = EchoBro()
    tool = None
    for candidate in await _service_server(bro, wire='mcp').list_tools():
      if candidate.name == 'summon':
        tool = candidate
    assert tool is not None
    # a generic exception is the agent-loop tool-error contract (vs ToolControlSignal)
    with pytest.raises(summon_module.SummonError, match='summon denied'):
      await tool.call({'target': 'dev', 'prompt': 'deploy'})


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
      tools: ClassVar = [mcp.shell('git status'), mcp.shell('git diff', 'git status')]

      def __init__(self):
        super().__init__(system_prompt='')

    selection = ShellBro()._selected_tools_for('bro')
    assert selection.shell_commands == ('git status', 'git diff')
    assert selection.shell_unrestricted is False

  def test_any_dominates_exact_commands(self):
    class ShellBro(BaseBro):
      name = 'shell-any'
      description = 'd'
      tools: ClassVar = [mcp.shell('git status'), mcp.shell(mcp.ANY)]

      def __init__(self):
        super().__init__(system_prompt='')

    selection = ShellBro()._selected_tools_for('bro')
    assert selection.shell_unrestricted is True

  def test_finite_claude_roster_gates_bash_and_monitor_and_returns_control(self):
    class ShellBro(BaseBro):
      name = 'shell-gated'
      description = 'd'
      tools: ClassVar = [claude.block(*claude.SHELL), mcp.shell('git status')]

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
      tools: ClassVar = [mcp.shell('git status')]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(
      ValueError, match='Bash is narrowed through the shell roster but never blocked'
    ):
      InvalidBro().blocked_tool_names('claude')

  def test_any_leaves_claude_native_shell_untouched(self):
    class ShellBro(BaseBro):
      name = 'shell-unrestricted'
      description = 'd'
      tools: ClassVar = [mcp.shell(mcp.ANY)]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ShellBro()
    assert bro.blocked_tool_names('claude') == ()
    assert bro.narrowed_tool_commands('claude') == {}

  def test_summon_watch_is_the_only_native_command_without_a_declaration(self, monkeypatch):
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    selection = EchoBro()._selected_tools_for('bro')
    assert selection.shell_commands == (bro_module.SUMMON_WATCH_COMMAND,)
    assert selection.shell_unrestricted is False


class TestJobServiceTools:
  class AnyShellBro(BaseBro):
    name = 'job-tools'
    description = 'd'
    tools: ClassVar = [mcp.shell(mcp.ANY)]

    def __init__(self):
      super().__init__(system_prompt='')

  async def _tools(
    self, *, wire: mcp.Wire = 'bare', run: Optional[StubRun] = None
  ) -> tuple[MCPServer, dict[str, Tool]]:
    server = _service_server(self.AnyShellBro(), run=run, wire=wire)
    return server, {tool.name: tool for tool in await server.list_tools()}

  @pytest.mark.asyncio
  async def test_mounting_follows_harness_and_wire(self):
    run = StubRun()
    bare_server, bare = await self._tools(run=run)
    mcp_server, served = await self._tools(wire='mcp')
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(bare_server.close)
      stack.callback(mcp_server.close)
      assert {'job', 'poll', 'kill', 'jobs', 'chill'} <= set(bare)
      assert {'job', 'poll', 'kill', 'jobs'} <= set(served)
      assert 'chill' not in served
      mode = served['job'].parameters['properties']['mode']
      assert set(mode['enum']) == {'fg', 'bg'}
      claude_server = bro_module._build_service_server(
        self.AnyShellBro(), include_raise=False, harness='claude', wire='mcp'
      )
      claude_names = {tool.name for tool in await claude_server.list_tools()}
      assert claude_names.isdisjoint({'job', 'poll', 'kill', 'jobs', 'chill'})

  @pytest.mark.asyncio
  async def test_exact_roster_rejects_appended_shell_syntax(self):
    class ExactShellBro(BaseBro):
      name = 'exact-shell'
      description = 'd'
      tools: ClassVar = [mcp.shell('printf allowed')]

      def __init__(self):
        super().__init__(system_prompt='')

    run = StubRun()
    server = _service_server(ExactShellBro(), run=run)
    tools = {tool.name: tool for tool in await server.list_tools()}
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      result = await tools['job'].call({'command': '  printf allowed  ', 'mode': 'fg'})
      assert result == 'exited (code 0)\nallowed'
      with pytest.raises(ValueError, match='must match exactly'):
        await tools['job'].call({'command': 'printf allowed; true', 'mode': 'fg'})

  @pytest.mark.asyncio
  async def test_foreground_job_interrupted_by_other_news_becomes_background(self):
    run = StubRun()
    server, tools = await self._tools(run=run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      run.registry.start('sleep 0.3', 'bg')
      result = await tools['job'].call(
        {'command': 'printf early; sleep 1; printf late', 'mode': 'fg', 'timeout_seconds': 5}
      )
      assert isinstance(result, str)
      assert result.startswith('running\nearly')
      assert "continues in bg mode; read on with poll(id='job-2')" in result
      assert run.registry.get('job-2').mode == 'bg'
      first = run.inbox.drain()
      assert first is not None and 'job-1' in first.job_ids

      chilled = await tools['chill'].call({'seconds': 5})
      assert isinstance(chilled, dict)
      assert chilled['woken'] is True
      second = run.inbox.drain()
      assert second is not None
      assert '[job-2 bg `printf early; sleep 1; printf late` exited (code 0)]' in second.text
      assert 'late' in second.text
      assert run.inbox.drain() is None

  @pytest.mark.asyncio
  async def test_foreground_timeout_becomes_background_and_names_its_clamp(self, monkeypatch):
    run = StubRun()
    server, tools = await self._tools(run=run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      monkeypatch.setattr(bro_module, '_JOB_WAIT_CAP_SECONDS', 0.05)
      result = await tools['job'].call({'command': 'sleep 0.3', 'mode': 'fg', 'timeout_seconds': 5})
      assert isinstance(result, str)
      assert result.startswith('running')
      assert "continues in bg mode; read on with poll(id='job-1')" in result
      assert '[timeout_seconds 5 clamped to 0.05]' in result
      assert run.registry.get('job-1').mode == 'bg'

  @pytest.mark.asyncio
  async def test_exit_during_foreground_settlement_is_consumed_once(self, monkeypatch):
    original = Job.settle_foreground

    def delayed_settlement(job: Job, limit: int) -> tuple[str, bool]:
      time.sleep(0.1)
      return original(job, limit)

    monkeypatch.setattr(Job, 'settle_foreground', delayed_settlement)
    run = StubRun()
    server, tools = await self._tools(run=run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      result = await tools['job'].call(
        {'command': 'sleep 0.05; printf done', 'mode': 'fg', 'timeout_seconds': 0.01}
      )
      assert result == 'exited (code 0)\ndone'
      assert run.inbox.drain() is None

  @pytest.mark.asyncio
  async def test_chill_refuses_without_a_live_job_and_names_its_clamp(self, monkeypatch):
    run = StubRun()
    server, tools = await self._tools(run=run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      with pytest.raises(ValueError, match='at least one live job'):
        await tools['chill'].call({})
      run.registry.start('sleep 0.3', 'bg')
      monkeypatch.setattr(bro_module, '_JOB_WAIT_CAP_SECONDS', 0.05)
      result = await tools['chill'].call({'seconds': 5})
      assert isinstance(result, dict)
      assert result['woken'] is False
      assert result['note'] == 'seconds 5 clamped to 0.05'

  @pytest.mark.asyncio
  async def test_cancelling_foreground_wait_leaves_a_background_job(self):
    run = StubRun()
    server, tools = await self._tools(run=run)
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      call = asyncio.create_task(
        tools['job'].call({'command': 'sleep 30', 'mode': 'fg', 'timeout_seconds': 60})
      )
      for _ in range(100):
        if len(run.registry.values()) > 0:
          break
        await asyncio.sleep(0.01)
      call.cancel()
      with pytest.raises(asyncio.CancelledError):
        await call
      [job] = run.registry.values()
      assert job.mode == 'bg'

  @pytest.mark.asyncio
  async def test_summon_watch_admission_mounts_a_single_command_shell(self, monkeypatch):
    monkeypatch.setenv(MAY_SUMMON_ENV, encode_may_summon(('reviewer',)))
    run = StubRun()
    server = _service_server(EchoBro(), run=run)
    tools = {tool.name: tool for tool in await server.list_tools()}
    with contextlib.ExitStack() as stack:
      stack.callback(run.registry.close)
      stack.callback(server.close)
      assert {'job', 'poll', 'kill', 'jobs', 'chill'} <= set(tools)
      started = await tools['job'].call({'command': bro_module.SUMMON_WATCH_COMMAND, 'mode': 'bg'})
      assert started == 'started job-1 (bg)'
      with pytest.raises(ValueError, match='must match exactly'):
        await tools['job'].call({'command': 'true', 'mode': 'bg'})

  @pytest.mark.asyncio
  async def test_mcp_service_owns_and_closes_its_registry(self, tmp_path):
    server, tools = await self._tools(wire='mcp')
    with contextlib.ExitStack() as stack:
      stack.callback(server.close)
      liveness = stack.enter_context(contextlib.closing(Liveness(tmp_path / 'job-liveness')))
      await tools['job'].call({'command': liveness.holding('sleep 30'), 'mode': 'bg'})
      await asyncio.to_thread(liveness.wait_started)
      listed = await tools['jobs'].call({})
      assert isinstance(listed, dict)
      [status] = listed['result']
      assert status['id'] == 'job-1'
      assert status['mode'] == 'bg'
      assert status['state'] == 'running'
      assert status['exit_code'] is None

      server.close()

      liveness.assert_reaped()
      with pytest.raises(RuntimeError, match='registry is closed'):
        await tools['job'].call({'command': 'true', 'mode': 'bg'})
