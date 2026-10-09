from pathlib import Path

import pytest

from bro import mcp, registry
from bro.base.condition import ConditionError, StringVariable, var, when
from bro.llm.mcp import InProcessMCPServer
from bro.mcp import render_text, select
from bros.bro import Bro


class _Reviewer(Bro):
  name = 'x-reviewer'
  description = 'a reviewer'


class _HouseReviewer(_Reviewer):
  name = 'x-house-reviewer'
  description = 'a reviewer of this house'


@pytest.fixture
def installed_reviewers(monkeypatch):
  """`x-house-reviewer`, derived from `x-reviewer`, installed for the test."""
  for cls in (_Reviewer, _HouseReviewer):
    monkeypatch.setitem(registry._REGISTRY, cls.name, cls)
  monkeypatch.setattr(mcp, '_persona_names', lambda: frozenset({'x-reviewer', 'x-house-reviewer'}))


class TestRenderText:
  def test_harness_branches(self):
    text = 'watch: {{iff #harness = bro}}job/watch{{eliff #harness = claude}}Monitor{{end}}'
    assert render_text(text, harness='bro') == 'watch: job/watch'
    assert render_text(text, harness='claude') == 'watch: Monitor'

  def test_harness_facts_cannot_shadow_framework_facts(self):
    class ConflictingHarness(mcp.Harness):
      name = 'conflicting'

      def facts(self):
        return {'harness': StringVariable('other')}

    with pytest.raises(ValueError, match='facts conflict with framework facts: harness'):
      render_text('{{insert #harness}}', harness=ConflictingHarness())

  def test_creds_membership_probes_availability(self, monkeypatch):
    monkeypatch.setattr(mcp.credentials, 'available', lambda name: name == 'openai')
    text = '{{iff #creds contains openai}}summarized{{else}}raw{{end}}'
    assert render_text(text, creds=['openai']) == 'summarized'
    text = '{{iff #creds contains github}}push{{else}}no push{{end}}'
    assert render_text(text, creds=['openai', 'github']) == 'no push'

  def test_creds_outside_universe_raises(self, monkeypatch):
    monkeypatch.setattr(mcp.credentials, 'available', lambda name: True)
    with pytest.raises(ValueError, match='universe'):
      render_text('{{iff #creds contains typo}}x{{else}}y{{end}}', creds=['openai'])

  def test_creds_probed_lazily(self, monkeypatch):
    # only the tested name resolves — a large universe costs nothing extra.
    probed: list[str] = []

    def available(name: str) -> bool:
      probed.append(name)
      return True

    monkeypatch.setattr(mcp.credentials, 'available', available)
    render_text('{{when #creds contains openai}}x{{end}}', creds=['openai', 'github', 'notion'])
    assert probed == ['openai']

  def test_absent_fact_raises_on_reference(self):
    with pytest.raises(ValueError, match='unknown variable #creds'):
      render_text('{{when #creds contains openai}}x{{end}}', harness='bro')

  def test_facts_combine(self):
    text = '{{when #harness = bro}}B{{end}}{{when #hold = guided}}H{{end}}'
    assert render_text(text, harness='bro', hold='guided') == 'BH'

  def test_plain_text_unchanged_without_consulting_availability(self, monkeypatch):
    def boom(name: str) -> bool:
      raise AssertionError('availability must not be consulted with no directive')

    monkeypatch.setattr(mcp.credentials, 'available', boom)
    assert render_text('plain text', creds=[]) == 'plain text'

  def test_well_formed_uninstalled_literal_reads_as_false(self):
    assert render_text('{{when #harness = codex}}x{{end}}', harness='claude') == ''

  def test_malformed_harness_literal_raises(self):
    with pytest.raises(ValueError, match='invalid literal'):
      render_text('{{when #harness = Claude}}x{{end}}', harness='claude')

  def test_well_formed_uninstalled_harness_argument_is_allowed(self):
    assert render_text('{{iff a = a}}x{{end}}', harness='gemini') == 'x'

  def test_malformed_harness_argument_raises(self):
    with pytest.raises(ValueError, match='invalid harness name'):
      render_text('{{iff a = a}}x{{end}}', harness='Gemini')

  def test_hold_fact_selects_a_branch(self):
    text = '{{iff #hold = unattended}}U{{else}}other{{end}}'
    assert render_text(text, hold='unattended') == 'U'
    assert render_text(text, hold='guided') == 'other'

  def test_may_summon_membership_is_the_supplied_list(self):
    text = '{{when #may_summon contains bro}}delegate{{end}}'
    assert render_text(text, may_summon=['bro']) == 'delegate'
    # 'bro' is an installed persona (the core entry point), so testing it
    # against an empty list reads as absent rather than raising
    assert render_text(text, may_summon=[]) == ''

  def test_may_summon_membership_is_is_a(self, installed_reviewers):
    text = '{{when #may_summon contains x-reviewer}}delegate{{end}}'
    assert render_text(text, may_summon=['x-house-reviewer']) == 'delegate'

  def test_may_summon_does_not_read_a_grant_as_the_bros_deriving_from_it(self, installed_reviewers):
    text = '{{when #may_summon contains x-house-reviewer}}delegate{{end}}'
    assert render_text(text, may_summon=['x-reviewer']) == ''

  def test_may_summon_universe_admits_a_granted_but_uninstalled_target(self):
    text = '{{when #may_summon contains ghost-bro}}delegate{{end}}'
    assert render_text(text, may_summon=['ghost-bro']) == 'delegate'

  def test_may_summon_outside_the_universe_raises(self):
    with pytest.raises(ValueError, match='universe'):
      render_text('{{when #may_summon contains not-a-bro}}x{{end}}', may_summon=['bro'])

  def test_absent_may_summon_raises_on_reference(self):
    with pytest.raises(ValueError, match='unknown variable #may_summon'):
      render_text('{{when #may_summon contains bro}}x{{end}}', harness='bro')

  def test_talk_membership_is_the_supplied_rights(self):
    text = '{{when #talk contains worker.question}}consult{{end}}'
    assert render_text(text, talk=['worker.question']) == 'consult'
    assert render_text(text, talk=[]) == ''

  def test_talk_rejects_an_unknown_supplied_right(self):
    with pytest.raises(ValueError, match='unknown talk right'):
      render_text('{{when #talk contains worker.say}}x{{end}}', talk=['summoned.sing'])

  def test_absent_talk_raises_on_reference(self):
    with pytest.raises(ValueError, match='unknown variable #talk'):
      render_text('{{when #talk contains worker.say}}x{{end}}', harness='bro')

  def test_hold_undefined_outside_hold_text(self):
    # the hold fact is supplied only when rendering the hold text, so a
    # stray #hold directive in hold-neutral text fails instead of picking a side
    with pytest.raises(ValueError, match='unknown variable #hold'):
      render_text('{{when #hold = unattended}}x{{end}}', harness='claude')

  def test_unknown_hold_argument_raises(self):
    with pytest.raises(ValueError, match='unknown hold'):
      render_text('{{iff a = a}}x{{end}}', hold='automatic')

  def test_include_resolves_through_prompts_loader(self, monkeypatch):
    from bro import prompts

    files = {'x.md': 'spliced {{iff #harness = bro}}B{{eliff #harness = claude}}C{{end}}'}
    monkeypatch.setattr(prompts, 'get_prompt', lambda name: files[name])
    assert render_text('root: {{include x.md}}', harness='bro') == 'root: spliced B'
    assert render_text('root: {{include x.md}}', harness='claude') == 'root: spliced C'

  def test_include_escaping_the_prompts_directory_raises(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      render_text('{{include ../AGENTS.md}}', harness='bro')


def _toolset(namespace: str, *tool_names: str) -> mcp.Toolset:
  toolset = mcp.Toolset(namespace)
  for name in tool_names:

    def tool() -> str:
      return 'ok'

    tool.__name__ = name
    toolset.tool(f'{name} tool')(tool)
  return toolset


class TestToolLayer:
  def test_a_layer_declares_something(self):
    with pytest.raises(ValueError, match='must declare a server or a reach entry'):
      mcp.ToolLayer()

  def test_layers_merge_into_one(self):
    layer = mcp.files() | mcp.web()
    assert [entry.key.name for entry in layer.reach_entries] == ['files', 'web']

  def test_a_raw_spec_is_keyed_by_the_namespace_it_serves(self):
    spec = mcp.MCPServerSpec(namespace='raw', build=lambda: InProcessMCPServer('raw', []))
    [entry] = mcp.ToolLayer(server_specs=(spec,)).reach_entries
    assert entry.key == mcp.ReachKey('server', 'raw')

  def test_a_spec_namespace_is_a_wire_segment(self):
    with pytest.raises(ValueError, match='double underscore'):
      mcp.MCPServerSpec(namespace='a__b', build=lambda: InProcessMCPServer('a__b', []))

  def test_entries_of_different_kinds_never_share_a_key(self):
    keys = [
      entry.key
      for layer in (mcp.mount(_toolset('man', 'read')), mcp.man('ride'), mcp.cli('bro list'))
      for entry in layer.reach_entries
    ]
    assert len(set(keys)) == len(keys)

  def test_brash_declares_quote_aware_command_patterns_or_any(self):
    [entry] = mcp.brash(' git log ... ', "grep -E 'a*b' ...").entries
    assert entry == mcp.Brash(commands=('git log ...', "grep -E 'a*b' ..."))
    assert mcp.brash(mcp.ANY).entries == (mcp.Brash(unrestricted=True),)

  @pytest.mark.parametrize(
    ('commands', 'error_type', 'message'),
    [
      ((), ValueError, 'needs at least one command'),
      (('',), ValueError, 'non-empty'),
      ((mcp.ANY, 'git status'), ValueError, 'ANY must be the only'),
      (('git status', 'git status'), ValueError, 'duplicate entries'),
    ],
  )
  def test_brash_rejects_invalid_command_lists(self, commands, error_type, message):
    with pytest.raises(error_type, match=message):
      mcp.brash(*commands)

  def test_mount_resolves_its_subset_at_declaration(self):
    toolset = _toolset('layer', 'read', 'write')
    assert mcp.mount(toolset).entries == (mcp.Mount(toolset, ('read', 'write')),)
    assert mcp.mount(toolset, 'read').entries == (mcp.Mount(toolset, ('read',)),)
    with pytest.raises(ValueError, match='unknown layer tools'):
      mcp.mount(toolset, 'nope')

  def test_cli_is_keyed_by_its_generated_tool_name(self):
    [entry] = mcp.cli('rewind show', 'trail_id').entries
    assert isinstance(entry, mcp.Cli)
    assert entry.key == mcp.ReachKey('cli', 'rewind_show')
    assert entry.spec.namespace == 'cli'

  def test_source_requires_a_data_source(self):
    with pytest.raises(TypeError, match='requires a DataSource'):
      mcp.source('dev-style')  # type: ignore[arg-type]

  def test_man_resolves_its_topic_at_declaration(self):
    [entry] = mcp.man('Dive-In').entries
    assert entry.key == mcp.ReachKey('man', 'dive-in')
    with pytest.raises(LookupError, match='dive-in'):
      mcp.man('no-such-topic')

  def test_revoke_withholds_the_keys_it_wraps_whatever_their_level(self):
    toolset = _toolset('pack', 'read', 'write')
    layer = mcp.revoke(mcp.files(write=False), mcp.mount(toolset, 'read'), mcp.cli('bro list'))
    assert [entry.key for entry in layer.reach_entries] == [
      mcp.ReachKey('group', 'files'),
      mcp.ReachKey('mount', 'pack'),
      mcp.ReachKey('cli', 'bro_list'),
    ]
    assert all(isinstance(entry, mcp.Revoked) for entry in layer.reach_entries)


class TestReduction:
  def test_files_levels_reduce_to_the_wider(self):
    narrow, wide = mcp.Files(write=False), mcp.Files()
    assert narrow.merge(wide) == wide
    assert wide.merge(narrow) == wide

  def test_command_lists_reduce_to_their_union(self):
    first = mcp.Brash(commands=('git log ...', 'gh pr view *'))
    second = mcp.Brash(commands=('gh pr view *', 'rewind show *'))
    assert first.merge(second) == mcp.Brash(
      commands=('git log ...', 'gh pr view *', 'rewind show *')
    )

  def test_any_absorbs_a_command_list(self):
    finite = mcp.Brash(commands=('git status',))
    unrestricted = mcp.Brash(unrestricted=True)
    assert finite.merge(unrestricted) == unrestricted
    assert unrestricted.merge(finite) == unrestricted

  def test_mounts_of_one_toolset_reduce_to_the_union_of_their_subsets(self):
    toolset = _toolset('pack', 'read', 'write', 'grep')
    merged = mcp.Mount(toolset, ('read', 'grep')).merge(mcp.Mount(toolset, ('write', 'read')))
    assert merged == mcp.Mount(toolset, ('read', 'grep', 'write'))

  def test_identical_entries_reduce_to_one(self):
    assert mcp.Web().merge(mcp.Web()) == mcp.Web()
    [first] = mcp.cli('bro show', 'name').entries
    [second] = mcp.cli('bro show', 'name').entries
    assert first.merge(second) == first

  def test_a_revoke_beside_a_grant_is_refused(self):
    [revoked] = mcp.revoke(mcp.delegation()).entries
    assert revoked.merge(mcp.Delegation()) is None
    assert mcp.Delegation().merge(revoked) is None

  def test_two_toolsets_under_one_namespace_are_refused(self):
    first, second = _toolset('pack', 'read'), _toolset('pack', 'read')
    assert mcp.Mount(first, ('read',)).merge(mcp.Mount(second, ('read',))) is None

  def test_two_different_sources_or_raw_specs_are_refused(self):
    from bro.datasources.file import FileSource

    page = FileSource('doc', summary='x', path=Path(__file__))
    other = FileSource('doc', summary='x', path=Path(__file__))
    assert mcp.Source(page).merge(mcp.Source(other)) is None
    first = mcp.MCPServerSpec(namespace='raw', build=lambda: InProcessMCPServer('raw', []))
    second = mcp.MCPServerSpec(namespace='raw', build=lambda: InProcessMCPServer('raw', []))
    assert mcp.Server(first).merge(mcp.Server(second)) is None

  def test_cli_entries_exposing_different_arguments_are_refused(self):
    [full] = mcp.cli('bro show').entries
    [narrowed] = mcp.cli('bro show', 'name').entries
    assert full.merge(narrowed) is None


class TestToolsetRendering:
  def _toolset(self) -> mcp.Toolset:
    toolset = mcp.Toolset('pack')

    @toolset.tool('read stuff{{when #tools contains manual}}; rules in manual{{end}}')
    def read(x: str) -> str:
      return x

    @toolset.tool('the shared rules')
    def manual() -> str:
      return 'rules'

    return toolset

  @pytest.mark.asyncio
  async def test_full_build_keeps_the_cross_reference(self):
    server = self._toolset().build()
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools['read'].description == 'read stuff; rules in manual'
    assert server.tool_universe == ('read', 'manual')

  @pytest.mark.asyncio
  async def test_scoped_build_drops_the_cross_reference(self):
    server = self._toolset().build('read')
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools['read'].description == 'read stuff'
    assert server.tool_universe == ('read', 'manual')

  def test_reference_outside_the_roster_raises_at_build(self):
    toolset = mcp.Toolset('pack')

    @toolset.tool('read stuff{{when #tools contains manaul}}; typo{{end}}')
    def read(x: str) -> str:
      return x

    with pytest.raises(ValueError, match='outside the set universe'):
      toolset.build()


class TestSelect:
  def test_harness_condition_filters_entries(self):
    entries = ['plain', when(var('harness') == 'bro', 'devtools')]
    assert select(entries, harness='bro') == ['plain', 'devtools']
    assert select(entries, harness='claude') == ['plain']

  def test_creds_fact_probes_availability(self, monkeypatch):
    monkeypatch.setattr(mcp.credentials, 'available', lambda name: name == 'openai')
    entries = [when(mcp.creds.contains('openai'), 'summary')]
    assert select(entries, creds=['openai']) == ['summary']
    monkeypatch.setattr(mcp.credentials, 'available', lambda name: False)
    assert select(entries, creds=['openai']) == []

  def test_talk_condition_filters_entries(self):
    entries = [when(var('talk').contains('worker.question'), 'consult')]
    assert select(entries, talk=['worker.question']) == ['consult']
    assert select(entries, talk=[]) == []

  def test_absent_fact_raises_on_reference(self):
    with pytest.raises(ConditionError, match='unknown variable #creds'):
      select([when(mcp.creds.contains('openai'), 'x')], harness='bro')

  def test_well_formed_uninstalled_harness_argument_is_allowed(self):
    assert select([], harness='gemini') == []

  def test_malformed_harness_argument_raises(self):
    with pytest.raises(ValueError, match='invalid harness name'):
      select([], harness='Gemini')


class TestWithoutMCPPackage:
  def test_declarations_defer_the_live_layer_until_build(self):
    import subprocess
    import sys

    code = (
      "import sys; sys.modules['mcp'] = None; "
      'import bro.mcp; '
      "assert 'bro.llm.mcp' not in sys.modules; "
      "toolset = bro.mcp.Toolset('probe'); "
      'bro.mcp.mount(toolset); '
      "assert 'bro.llm.mcp' not in sys.modules; "
      "print('ok')"
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'

  def test_layer_imports_without_the_mcp_package(self):
    # only FunctionTool may reach for the `mcp` package; simulate its absence in a
    # fresh subprocess.
    import subprocess
    import sys

    code = (
      "import sys; sys.modules['mcp'] = None; "
      'import bro.llm.llm, bro.llm.llms.openai, bro.llm.mcp, bro.mcp, bro.prompts; '
      "bro.mcp.render_text('plain'); "
      "print('ok')"
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


class TestServerTeardown:
  def test_toolset_close_releases_the_built_state(self):
    class _State:
      def __init__(self):
        self.closed = False

      def close(self) -> None:
        self.closed = True

    states: list[_State] = []

    def make_state() -> _State:
      states.append(_State())
      return states[-1]

    toolset = mcp.Toolset('probe', state=make_state, close=_State.close)

    @toolset.tool('does nothing')
    def noop() -> str:
      return 'ok'

    server = toolset.build()
    assert [state.closed for state in states] == [False]
    server.close()
    assert [state.closed for state in states] == [True]

  def test_close_is_a_no_op_without_declared_teardown(self):
    InProcessMCPServer('probe', []).close()
