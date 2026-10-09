import asyncio
import contextlib
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

import bro.bro as bro_module
import bro.llm.llms.echo as llm_llms_echo
import bro.mcp as mcp
import bro.workspace.banner as workspace_banner
from bro import brash_policy, watches
from bro.base import credentials
from bro.base.condition import ConditionError, iff, var, when
from bro.brash import REFUSED_STATUS
from bro.bro import BaseBro, feature
from bro.broker.environment import BROKER_TALK
from bro.datasources.file import FileSource
from bro.datasources.man import ManSource
from bro.datasources.searchable import Hit, SearchableDataSource
from bro.harness import Harness, Service, SessionEndReason
from bro.llm.mcp import FunctionTool, InProcessMCPServer, MCPServer
from bro.llm.tracker import ToolStepSource
from bro.mcp import MCPServerSpec, describe
from bro.monitor import SESSION_DIR_ENV
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
    self.brash_policy: Optional[Path] = None


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
      tools: ClassVar = [mcp.source(_StubSource())]

      def __init__(self):
        super().__init__(system_prompt='hi')

    bro = SourceBro()
    servers = bro._live_mcp_servers('bro')
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
      tools: ClassVar = [mcp.source(_StubSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    class ChildSourceBro(ParentSourceBro):
      name = 'child-sources'
      tools: ClassVar = [mcp.source(_MarkerSource())]

    bro = ChildSourceBro()
    assert [ds.name for ds in bro.reach().sources] == ['stub', 'marker']

  def test_man_pages_fold_into_one_manual_along_the_mro(self):
    page = FileSource('alpha', summary='the alpha page', path=Path(__file__))
    other = FileSource('beta', summary='the beta page', path=Path(__file__))

    class ParentManBro(BaseBro):
      name = 'parent-man'
      description = 'd'
      tools: ClassVar = [_man(page), mcp.source(_StubSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    class ChildManBro(ParentManBro):
      name = 'child-man'
      # a page both classes declare is one key, decided by the nearer class
      tools: ClassVar = [_man(other), _man(page)]

    bro = ChildManBro()
    # the manual sits where the first page was declared, ahead of the stub
    sources = bro.reach().sources
    assert [ds.name for ds in sources] == ['man', 'stub']
    folded = sources[0]
    assert isinstance(folded, ManSource)
    assert [p.name for p in folded.pages] == ['alpha', 'beta']

  def test_man_pages_gate_on_conditions_like_any_source(self):
    page = FileSource('alpha', summary='the alpha page', path=Path(__file__))

    def gated(enabled: bool) -> BaseBro:
      class GatedManBro(BaseBro):
        name = 'gated-man'
        description = 'd'
        tools: ClassVar = [when(enabled, _man(page))]

        def __init__(self):
          super().__init__(system_prompt='base')

      return GatedManBro()

    assert gated(False).reach().sources == ()
    assert [ds.name for ds in gated(True).reach().sources] == ['man']

  def test_data_source_summary_in_system_prompt(self):
    class SourceBro(BaseBro):
      name = 'summary-bro'
      description = 'd'
      tools: ClassVar = [mcp.source(_StubSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = SourceBro()
    assert '## Data sources' in bro.composed_prompt('bro')
    assert '**stub**' in bro.composed_prompt('bro')
    assert 'a stub data source for tests' in bro.composed_prompt('bro')
    # canonical `::` in the data-source block, resolved by the tool-names rule;
    # the example derives from the bro's own first source
    assert 'stub-source::' in bro.composed_prompt('bro')

  def test_summary_feature_directive_rendered_present(self, monkeypatch):
    from bro.base import credentials

    monkeypatch.setattr(credentials, 'available', lambda name: True)

    class MarkBro(BaseBro):
      name = 'mark-on'
      description = 'd'
      tools: ClassVar = [mcp.source(_MarkerSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    prompt = MarkBro().composed_prompt('bro')
    assert 'query summary on' in prompt
    assert 'no key' not in prompt
    assert '{{' not in prompt  # markers fully resolved, never leak raw

  def test_summary_feature_directive_rendered_absent(self, monkeypatch):
    from bro.base import credentials

    monkeypatch.setattr(credentials, 'available', lambda name: False)

    class MarkBro(BaseBro):
      name = 'mark-off'
      description = 'd'
      tools: ClassVar = [mcp.source(_MarkerSource())]

      def __init__(self):
        super().__init__(system_prompt='base')

    prompt = MarkBro().composed_prompt('bro')
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

    prompt = ToolBro().composed_prompt('bro')
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
    assert '# Tool names' in bro.composed_prompt('bro')
    assert '`namespace__tool`' in bro.composed_prompt('bro')
    assert '`mcp__namespace__tool`' not in bro.composed_prompt('bro')

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
  return _server_layer(MCPServerSpec(namespace='test', build=lambda: _make_server(*tool_names)))


def _man(page: FileSource) -> mcp.ToolLayer:
  return mcp.ToolLayer(entries=(mcp.Man(page),))


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
    with pytest.raises(TypeError, match=r"SourceTypoBro\.datasources.*move them to 'tools'"):

      class SourceTypoBro(BaseBro):
        datasources: ClassVar = [when(False, _StubSource())]

  def test_data_sources_names_its_fold_into_tools(self):
    with pytest.raises(TypeError, match=r"'data_sources' folded into 'tools'.*source\(\.\.\.\)"):

      class RetiredSourceBro(BaseBro):
        data_sources: ClassVar = [mcp.source(_StubSource())]

  def test_a_bare_data_source_in_tools_names_its_constructor(self):
    class BareSourceBro(BaseBro):
      name = 'bare-source'
      description = 'd'
      tools: ClassVar = [_StubSource()]  # type: ignore[list-item]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(
      TypeError, match=r'BareSourceBro\.tools\[0\] is a data source.*source\(\.\.\.\)'
    ):
      BareSourceBro()

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
    tools = await bro._live_mcp_servers('bro')[0].list_tools()
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
      tools: ClassVar = [_server_layer(MCPServerSpec(namespace='test', build=build))]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = CountBro()
    # metadata surfaces never build the live server
    bro.needed_secrets('bro')
    assert calls == 0
    first = bro._live_mcp_servers('bro')
    assert calls == 1
    assert bro._live_mcp_servers('bro') is first
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
      tools: ClassVar = [mcp.mount(toolset)]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ToolsetBro()
    assert len(bro.reach().server_specs) == 1
    tools = await bro._live_mcp_servers('bro')[0].list_tools()
    assert {tool.name for tool in tools} == {'ping'}


def _toolset(namespace: str, *tool_names: str) -> mcp.Toolset:
  toolset = mcp.Toolset(namespace)
  for name in tool_names:

    def tool() -> str:
      return 'ok'

    tool.__name__ = name
    toolset.tool(f'{name} tool')(tool)
  return toolset


_PACK = _toolset('pack', 'read', 'write')


class TestReachFold:
  def test_each_key_resolves_to_the_nearest_class_declaring_it(self):
    class Base(BaseBro):
      name = 'fold-base'
      description = 'd'
      tools: ClassVar = [mcp.files(), mcp.mount(_PACK), mcp.brash('git log ...')]

      def __init__(self):
        super().__init__(system_prompt='')

    class Derived(Base):
      name = 'fold-derived'
      tools: ClassVar = [mcp.web(), mcp.brash('git status')]

    reach = Derived().reach()
    assert [str(group) for group in reach.groups] == ['files', "brash('git status')", 'web']
    assert [spec.namespace for spec in reach.server_specs] == ['pack']

  def test_a_subclass_narrows_what_its_base_grants(self):
    class Base(BaseBro):
      name = 'narrow-base'
      description = 'd'
      tools: ClassVar = [mcp.files(), mcp.brash('git log ...', 'gh pr view *')]

      def __init__(self):
        super().__init__(system_prompt='')

    class Narrowed(Base):
      name = 'narrowed'
      tools: ClassVar = [mcp.files(write=False), mcp.brash('git log ...')]

    reach = Narrowed().reach()
    assert reach.files == mcp.Files(write=False)
    assert reach.brash == mcp.Brash(commands=('git log ...',))

  def test_one_class_reduces_its_own_entries_by_kind(self):
    class Reducing(BaseBro):
      name = 'reducing'
      description = 'd'
      tools: ClassVar = [
        mcp.files(write=False),
        mcp.brash('git status'),
        mcp.mount(_PACK, 'read'),
        mcp.files() | mcp.brash('git diff'),
        mcp.mount(_PACK, 'write'),
        mcp.web(),
        mcp.web(),
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    reach = Reducing().reach()
    assert reach.files == mcp.Files()
    assert reach.brash == mcp.Brash(commands=('git status', 'git diff'))
    assert reach.web == mcp.Web()
    [spec] = reach.server_specs
    assert [tool.name for tool in asyncio.run(spec.build().list_tools())] == ['read', 'write']

  def test_any_absorbs_a_command_list_declared_beside_it(self):
    class Unrestricted(BaseBro):
      name = 'brash-any'
      description = 'd'
      tools: ClassVar = [mcp.brash('git status'), mcp.brash(mcp.ANY)]

      def __init__(self):
        super().__init__(system_prompt='')

    assert Unrestricted().reach().brash == mcp.Brash(unrestricted=True)

  @pytest.mark.parametrize(
    'entries',
    [
      pytest.param(lambda: [mcp.delegation(), mcp.revoke(mcp.delegation())], id='revoke-and-grant'),
      pytest.param(lambda: [mcp.cli('bro show'), mcp.cli('bro show', 'name')], id='two-clis'),
      pytest.param(
        lambda: [mcp.source(_StubSource()), mcp.source(_StubSource())], id='two-sources'
      ),
      pytest.param(lambda: [_make_layer('a'), _make_layer('a')], id='two-raw-specs'),
    ],
  )
  def test_a_pair_that_does_not_reduce_fails_construction(self, entries):
    class Conflicting(BaseBro):
      name = 'conflicting'
      description = 'd'
      tools: ClassVar = entries()

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ValueError, match=r'Conflicting\.tools declares .* under one key'):
      Conflicting()

  def test_a_revoke_withholds_and_a_descendant_grants_again(self):
    class Base(BaseBro):
      name = 'revoke-base'
      description = 'd'
      tools: ClassVar = [mcp.delegation(), mcp.mount(_PACK), mcp.files()]

      def __init__(self):
        super().__init__(system_prompt='')

    class Revoking(Base):
      name = 'revoking'
      tools: ClassVar = [mcp.revoke(mcp.delegation(), mcp.mount(_PACK, 'read'))]

    class Regranting(Revoking):
      name = 'regranting'
      tools: ClassVar = [mcp.delegation()]

    revoked = Revoking().reach()
    assert revoked.delegation is None
    assert revoked.server_specs == ()
    assert revoked.files == mcp.Files()
    regranted = Regranting().reach()
    assert regranted.delegation == mcp.Delegation()
    assert regranted.server_specs == ()

  def test_an_unmet_condition_leaves_the_base_entry_in_force(self):
    class Base(BaseBro):
      name = 'conditional-base'
      description = 'd'
      tools: ClassVar = [mcp.files(), mcp.web()]

      def __init__(self):
        super().__init__(system_prompt='')

    def conditional(*, narrow: bool, offline: bool) -> mcp.Reach:
      class Conditional(Base):
        name = 'conditional'
        tools: ClassVar = [
          when(narrow, mcp.files(write=False)),
          when(offline, mcp.revoke(mcp.web())),
        ]

      return Conditional().reach()

    unmet = conditional(narrow=False, offline=False)
    assert (unmet.files, unmet.web) == (mcp.Files(), mcp.Web())
    narrowed = conditional(narrow=True, offline=False)
    assert (narrowed.files, narrowed.web) == (mcp.Files(write=False), mcp.Web())
    offline = conditional(narrow=False, offline=True)
    assert (offline.files, offline.web) == (mcp.Files(), None)

  @pytest.mark.asyncio
  async def test_a_subclass_replaces_one_cli_entry_and_keeps_its_sibling(self):
    class Base(BaseBro):
      name = 'cli-base'
      description = 'd'
      tools: ClassVar = [mcp.cli('bro show'), mcp.cli('bro list')]

      def __init__(self):
        super().__init__(system_prompt='')

    class Replacing(Base):
      name = 'cli-replacing'
      tools: ClassVar = [mcp.cli('bro show', 'name')]

    parameters = {}
    for spec in Replacing().reach().server_specs:
      [tool] = await spec.build().list_tools()
      parameters[tool.name] = set(tool.parameters['properties'])
    assert set(parameters) == {'bro_show', 'bro_list'}
    assert 'name' in parameters['bro_show']
    assert 'system_prompt' not in parameters['bro_show']

  def test_a_key_two_bases_declare_resolves_to_the_first_in_the_mro(self):
    class ReadOnly(BaseBro):
      name = 'read-only-base'
      description = 'd'
      tools: ClassVar = [mcp.files(write=False)]

    class Writing(BaseBro):
      name = 'writing-base'
      description = 'd'
      tools: ClassVar = [mcp.files(), mcp.web()]

    class ReadOnlyFirst(ReadOnly, Writing):
      name = 'read-only-first'

    class WritingFirst(Writing, ReadOnly):
      name = 'writing-first'

    assert ReadOnlyFirst().reach().files == mcp.Files(write=False)
    assert ReadOnlyFirst().reach().web == mcp.Web()
    assert WritingFirst().reach().files == mcp.Files()

  def test_a_declaration_cannot_condition_on_the_harness(self):
    class HarnessGated(BaseBro):
      name = 'harness-gated'
      description = 'd'
      tools: ClassVar = [when(var('harness') == 'bro', mcp.files())]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(ConditionError, match='unknown variable #harness'):
      HarnessGated()


class TestConditionalComponents:
  def test_unmet_server_excluded_and_never_built(self):
    def build():
      raise AssertionError('an unmatched spec must never build')

    class CondBro(BaseBro):
      name = 'cond'
      description = 'd'
      tools: ClassVar = [when(False, _server_layer(MCPServerSpec(namespace='test', build=build)))]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = CondBro()
    assert bro.reach().server_specs == ()
    assert bro._live_mcp_servers('bro') == []

  def test_bool_condition_is_a_constant(self):
    class BoolBro(BaseBro):
      name = 'bool'
      description = 'd'
      tools: ClassVar = [when(False, _make_layer('a')), when(True, _make_layer('b'))]

      def __init__(self):
        super().__init__(system_prompt='')

    assert len(BoolBro().reach().server_specs) == 1

  def test_unmet_data_source_excluded_everywhere(self):
    class CondSourceBro(BaseBro):
      name = 'cond-source'
      description = 'd'
      tools: ClassVar = [when(False, mcp.source(_SecretSource()))]

      def __init__(self):
        super().__init__(system_prompt='base')

    bro = CondSourceBro()
    assert bro.reach().sources == ()
    assert '## Data sources' not in bro.composed_prompt('bro')
    assert bro.needed_secrets('bro') == ()
    assert bro._live_mcp_servers('bro') == []


class TestFeatures:
  @pytest.fixture(autouse=True)
  def _register_xkey(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.known_names', lambda: frozenset({'xkey'}))

  def _bro_class(self):
    class FeatureBro(BaseBro):
      name = 'feature-bro'
      description = 'd'
      features: ClassVar = {'x': mcp.creds.contains('xkey')}
      tools: ClassVar = [
        when(feature('x'), _server_layer(MCPServerSpec.of('secret', _SecretServer)))
      ]
      system_prompt = 'base text{{when #features contains x}} FEATURE TEXT{{end}}'

    return FeatureBro

  def test_gated_component_and_text_follow_the_gates(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'xkey')
    on = self._bro_class()()
    assert len(on.reach().server_specs) == 1
    assert 'FEATURE TEXT' in on.composed_prompt('bro')
    assert set(on.needed_secrets('bro')) == {'alpha', 'beta'}

    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
    off = self._bro_class()()
    assert off.reach().server_specs == ()
    assert 'FEATURE TEXT' not in off.composed_prompt('bro')
    assert off.needed_secrets('bro') == ()

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
    assert len(child.reach().server_specs) == 1
    assert 'FEATURE TEXT' in child.composed_prompt('bro')

  def test_derived_disables_parent_feature(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'xkey')

    class Disabled(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': False}

    child = Disabled()
    assert child.reach().server_specs == ()
    assert 'FEATURE TEXT' not in child.composed_prompt('bro')

  def test_gate_credential_is_tiered_with_the_feature(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
    gated = self._bro_class()()
    assert 'xkey' in gated.optional_secrets('bro')
    assert 'xkey' not in gated.needed_secrets('bro')

    class Pinned(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': True}

    pinned = Pinned()
    assert 'xkey' in pinned.needed_secrets('bro')
    assert 'xkey' not in pinned.optional_secrets('bro')

    class Disabled(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': False}

    disabled = Disabled()
    assert 'xkey' not in disabled.needed_secrets('bro')
    assert 'xkey' not in disabled.optional_secrets('bro')

  def test_regating_a_feature_replaces_its_credential(self, monkeypatch):
    monkeypatch.setattr('bro.base.credentials.known_names', lambda: frozenset({'xkey', 'ykey'}))
    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)

    class Regated(self._bro_class()):
      name = 'feature-child'
      features: ClassVar = {'x': mcp.creds.contains('ykey')}

    regated = Regated()
    assert 'ykey' in regated.optional_secrets('bro')
    assert 'xkey' not in regated.optional_secrets('bro')

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

    assert StillDisabled().reach().server_specs == ()

  def test_gate_may_reference_only_the_gate_vocabulary(self):
    class SurfaceGated(BaseBro):
      name = 'surface-gated'
      description = 'd'
      features: ClassVar = {'x': var('harness') == 'bro'}
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


class _FilesHarness(Harness):
  """serves `files` with a server of its own, and leaves `web` unserved."""

  name = 'files-serving'

  def serve(self, reach):
    specs = ()
    if reach.files is not None:
      specs = (MCPServerSpec.of('secret', _SecretServer),)
    unserved = ('web',) if reach.web is not None else ()
    return Service(server_specs=specs, unserved=unserved)


class TestHarnessService:
  def _bro(self):
    class ServedBro(BaseBro):
      name = 'served'
      description = 'd'
      tools: ClassVar = [mcp.files(), mcp.web(), _make_layer('a')]

      def __init__(self):
        super().__init__(system_prompt='')

    return ServedBro()

  def test_a_harness_mounts_the_servers_it_serves_the_groups_with(self):
    servers = self._bro().assemble(harness=_FilesHarness(), include_raise=False)
    assert [server.namespace for server in servers] == ['secret', 'test', 'bro']

  def test_the_manifest_follows_what_the_harness_serves(self):
    bro = self._bro()
    assert set(bro.needed_secrets(harness=_FilesHarness())) == {'alpha', 'beta'}
    assert bro.needed_secrets(harness=Harness('plain')) == ()

  def test_a_harness_serving_nothing_leaves_every_group_unserved(self):
    assert Harness('plain').serve(self._bro().reach()).unserved == ('files', 'web')

  def test_each_harness_gets_the_servers_built_for_it(self):
    bro = self._bro()
    served = bro._live_mcp_servers(_FilesHarness())
    plain = bro._live_mcp_servers(Harness('plain'))
    assert [server.namespace for server in plain] == ['test']
    assert bro._live_mcp_servers(_FilesHarness()) is served

  def test_service_server_carries_banner_but_not_raise(self):
    names = asyncio.run(
      _collect_tool_names(self._bro().assemble(harness='claude', include_raise=False))
    )
    # `raise` is gated on the session hold (not unattended here — no BRO_HOLD);
    # the environment facts stay available as `banner`
    assert 'banner' in names
    assert 'raise' not in names


class TestNeededSecrets:
  def test_unions_mcp_datasources_and_extra(self):
    class ManifestBro(BaseBro):
      name = 'manifest'
      description = 'd'
      tools: ClassVar = [
        _server_layer(MCPServerSpec.of('secret', _SecretServer)),
        mcp.source(_SecretSource()),
      ]
      extra_secrets = ('delta',)

      def __init__(self):
        super().__init__(system_prompt='')

    bro = ManifestBro()
    # the llm key is NOT in needed_secrets() — surfaces that run the bro add it
    assert bro.needed_secrets('bro') == ('alpha', 'beta', 'delta', 'gamma')
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

    assert {'one', 'two'} <= set(Derived().needed_secrets('bro'))


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
    spec = MCPServerSpec(namespace='test', build=lambda: _make_server('probe'), **manifest)

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
      tools: ClassVar = [mcp.source(InstanceSource())]

      def __init__(self):
        super().__init__(system_prompt='')

    with pytest.raises(
      ValueError,
      match=rf'InstanceBro\.tools\[0\] InstanceSource\.{manifest_name}.*github\+reviewer',
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

    assert Bare().needed_secrets('bro') == ()


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

    assert OptionalBro().optional_secrets('bro') == ('gamma',)

  def test_unions_mcp_and_datasource_optional(self):
    class OptBro(BaseBro):
      name = 'opt'
      description = 'd'
      tools: ClassVar = [
        _server_layer(MCPServerSpec.of('optional-srv', _OptionalServer)),
        mcp.source(_OptionalSource()),
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = OptBro()
    assert bro.optional_secrets('bro') == ('omega', 'psi')

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
      tools: ClassVar = [
        _server_layer(MCPServerSpec.of('both-srv', _BothServer)),
        mcp.source(_OptShared()),
      ]

      def __init__(self):
        super().__init__(system_prompt='')

    bro = BothBro()
    assert 'shared' in bro.needed_secrets('bro')
    assert bro.optional_secrets('bro') == ()

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
      tools: ClassVar = [mcp.source(OptionalSource())]

    optional_bro = OptionalBro()
    assert optional_bro.optional_secrets('bro') == ('gamma',)
    assert optional_bro.missing_secrets('bro') == ()


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
    prompt = bro.system_prompt_for(hold='unattended', harness='bro')
    assert '`bro::raise`' in prompt
    assert 'unclear' in prompt
    assert bro.composed_prompt('bro') in prompt
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
    prompt = bro.system_prompt_for(hold='guided', harness='bro')
    assert 'clarifying question' in prompt
    assert bro.composed_prompt('bro') in prompt
    assert '# Guided session' in prompt
    assert '# Unattended session' not in prompt

  def test_native_system_prompt_passes_the_runs_talk_to_the_summoned_contract(self, monkeypatch):
    monkeypatch.setenv(SUMMONED_ENV, '1')
    monkeypatch.setenv(BROKER_TALK, 'worker.question')
    prompt = EchoBro().system_prompt_for(hold='unattended', harness='bro')
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

    assert ShellBro().reach().brash == mcp.Brash(commands=('git status', 'git diff'))

  def test_summoning_does_not_add_an_undeclared_shell_command(self, monkeypatch):
    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))
    assert EchoBro().reach().brash is None


def _exited_batch(store: watches.Store, command: str) -> list[str]:
  """the lines `command`'s watch left once its producer exited."""
  watch = watches.Watch(command, store.directory, watches.slug(command))
  deadline = time.monotonic() + 10
  while watch.producer_alive():
    assert time.monotonic() < deadline, f'`{command}` did not exit'
    time.sleep(0.01)
  batch = store.take()
  assert batch is not None
  return batch.splitlines()


class _ListedBro(BaseBro):
  name = 'listed-watch-brash'
  description = 'd'
  tools: ClassVar = [mcp.brash('printf ...')]

  def __init__(self):
    super().__init__(system_prompt='')


class _UnrestrictedBro(BaseBro):
  name = 'unrestricted-watch-brash'
  description = 'd'
  tools: ClassVar = [mcp.brash(mcp.ANY)]

  def __init__(self):
    super().__init__(system_prompt='')


class TestWatchServiceTools:
  @pytest.mark.asyncio
  async def test_a_finite_command_list_runs_watch_lines_in_brash(self, tmp_path):
    declaration = _ListedBro()
    with watches.Owner.temporary() as owner:
      run = StubRun()
      run.watch_store = owner.store
      run.brash_policy = brash_policy.write(tmp_path, declaration.reach())
      server = _service_server(declaration, run=run)
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        # an escaped trailing space is part of the line's last word
        listed = "printf '<%s>\\n' x\\ "
        assert await tools['watch'].call({'command': listed}) == f'watching `{listed}`'
        assert _exited_batch(owner.store, listed) == [
          f'[{listed}] <x >',
          f'[{listed}] [watch-run] exited 0',
        ]
        refused = 'printf listed; cat /dev/null'
        await tools['watch'].call({'command': refused})
        refusal, exit_line = _exited_batch(owner.store, refused)
        assert refusal.startswith(f"[{refused}] brash: refused 'cat'")
        assert exit_line == f'[{refused}] [watch-run] exited {REFUSED_STATUS}'

  @pytest.mark.asyncio
  async def test_a_session_without_a_live_run_watches_under_its_published_policy(
    self, monkeypatch, tmp_path
  ):
    declaration = _ListedBro()
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / 'session'))
    policy = brash_policy.write(tmp_path, declaration.reach())
    monkeypatch.setenv(brash_policy.POLICY_ENV, str(policy))
    with watches.Owner.for_session() as owner:
      server = bro_module._build_service_server(
        declaration, include_raise=False, harness='claude', live_run=None
      )
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        await tools['watch'].call({'command': 'cat /dev/null'})
        refusal, _ = _exited_batch(owner.store, 'cat /dev/null')
        assert refusal.startswith("[cat /dev/null] brash: refused 'cat'")

  @pytest.mark.asyncio
  async def test_an_unrestricted_shell_runs_watch_lines_in_bash(self):
    with watches.Owner.temporary() as owner:
      run = StubRun()
      run.watch_store = owner.store
      server = _service_server(_UnrestrictedBro(), run=run)
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        await tools['watch'].call({'command': 'echo $((1 + 1))'})
        assert _exited_batch(owner.store, 'echo $((1 + 1))') == [
          '[echo $((1 + 1))] 2',
          '[echo $((1 + 1))] [watch-run] exited 0',
        ]

  @pytest.mark.asyncio
  async def test_unwatch_stops_a_watch_whose_command_the_list_does_not_admit(self):
    with watches.Owner.temporary() as owner:
      run = StubRun()
      run.watch_store = owner.store
      server = _service_server(_ListedBro(), run=run)
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        watch = owner.store.start('sleep 60')
        assert await tools['unwatch'].call({'command': 'sleep 60'}) == (
          'stopped watching `sleep 60`'
        )
        assert not watch.producer_alive()

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

  @pytest.mark.asyncio
  async def test_watch_starts_a_watch_that_wakes_on_quiet_lines(self):
    class QuietShellBro(BaseBro):
      name = 'quiet-watch-shell'
      description = 'd'
      tools: ClassVar = [mcp.brash('printf allowed')]

      def __init__(self):
        super().__init__(system_prompt='')

    with watches.Owner.temporary() as owner:
      run = StubRun()
      run.watch_store = owner.store
      server = _service_server(QuietShellBro(), run=run, harness=Harness('alternate'))
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        await tools['watch'].call({'command': 'printf allowed', 'wake_on_quiet': True})
        (started,) = owner.store.declared()
        assert started.wakes_on_quiet()

  @pytest.mark.asyncio
  async def test_quest_watch_mounts_only_where_the_session_may_summon(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    assert 'quest_watch' not in await _collect_tool_names([_service_server(EchoBro())])

    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))
    assert 'quest_watch' in await _collect_tool_names([_service_server(EchoBro())])

  @pytest.mark.asyncio
  async def test_quest_watch_sets_the_session_watch_to_wake_on_quiet_lines(self, monkeypatch):
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://token@127.0.0.1:9')
    monkeypatch.setenv(LAUNCH_ENV, encode_launch({'bro': {'bros': frozenset({'reviewer'})}}))
    with watches.Owner.temporary() as owner:
      session_watch = watches.Watch(
        watches.SESSION_WATCH_COMMAND,
        owner.store.directory,
        watches.slug(watches.SESSION_WATCH_COMMAND),
      )
      session_watch.command_file.write_text(f'{watches.SESSION_WATCH_COMMAND}\n')
      session_watch.log.write_text(f'{watches.quiet("summon started (quest Q1 to reviewer)")}\n')
      run = StubRun()
      run.watch_store = owner.store
      server = _service_server(EchoBro(), run=run)
      with contextlib.closing(server):
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert await tools['quest_watch'].call({'wake_on_quiet': True}) == {'wake_on_quiet': True}
        assert owner.store.has_waking_lines()
        assert await tools['quest_watch'].call({'wake_on_quiet': False}) == {'wake_on_quiet': False}
        assert not owner.store.has_waking_lines()


def test_selected_claude_composition_imports_no_other_harness():
  probe = textwrap.dedent("""
    import sys
    from unittest.mock import patch
    from bro.harness import get_harness
    from bro.registry import create_bro, register
    from bro.bro import BaseBro
    from ride.claude.system_prompt import session_append_prompt

    harness = get_harness('claude')
    assert 'bro.native.harness' not in sys.modules
    with patch('bro.bro._load_shared_prompts', side_effect=AssertionError('eager composition')):
        create_bro('bro')
    assert 'bro.native.harness' not in sys.modules

    class SelectedBro(BaseBro):
        name = 'selected-probe'
        description = 'selected harness probe'
        system_prompt = '{{iff #harness = claude}}selected prompt{{end}}'

    register(SelectedBro)
    bro = create_bro('selected-probe')
    prompt = bro.composed_prompt(harness)
    assert 'selected prompt' in prompt
    assert 'bro::skill' not in prompt
    assert 'selected prompt' in session_append_prompt('unattended', 'selected-probe')
    assert 'bro.native.harness' not in sys.modules
  """)
  subprocess.run([sys.executable, '-c', probe], check=True, capture_output=True, text=True)
