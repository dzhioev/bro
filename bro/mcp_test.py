import pytest

from bro import mcp, registry
from bro.base.condition import ConditionError, when
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

  def test_wire_branches(self):
    text = 'call {{iff #wire = bare}}ns__tool{{eliff #wire = mcp}}mcp__ns__tool{{end}}'
    assert render_text(text, wire='bare') == 'call ns__tool'
    assert render_text(text, wire='mcp') == 'call mcp__ns__tool'

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
    with pytest.raises(ValueError, match='unknown variable #wire'):
      render_text('{{when #wire = bare}}x{{end}}', harness='bro')

  def test_facts_combine(self):
    text = '{{when #harness = bro}}B{{end}}{{when #wire = mcp}}M{{end}}'
    assert render_text(text, harness='bro', wire='mcp') == 'BM'

  def test_plain_text_unchanged_without_consulting_availability(self, monkeypatch):
    def boom(name: str) -> bool:
      raise AssertionError('availability must not be consulted with no directive')

    monkeypatch.setattr(mcp.credentials, 'available', boom)
    assert render_text('plain text', creds=[]) == 'plain text'

  def test_unknown_literal_raises(self):
    with pytest.raises(ValueError, match='domain'):
      render_text('{{when #harness = claud}}x{{end}}', harness='claude')

  def test_unknown_harness_argument_raises(self):
    with pytest.raises(ValueError, match='unknown harness'):
      render_text('{{iff a = a}}x{{end}}', harness='gemini')  # type: ignore[arg-type]

  def test_unknown_wire_argument_raises(self):
    with pytest.raises(ValueError, match='unknown wire'):
      render_text('{{iff a = a}}x{{end}}', wire='grpc')  # type: ignore[arg-type]

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

  def test_hold_undefined_outside_hold_text(self):
    # the hold fact is supplied only when rendering the hold text, so a
    # stray #hold directive in hold-neutral text fails instead of picking a side
    with pytest.raises(ValueError, match='unknown variable #hold'):
      render_text('{{when #hold = unattended}}x{{end}}', harness='claude', wire='mcp')

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


class TestToolLayer:
  @pytest.mark.parametrize(
    ('tool_names', 'error_type', 'message'),
    [
      ((), ValueError, 'must mount a server, block a native tool, narrow one, or serve one'),
      (('',), TypeError, 'non-empty strings'),
      (('Read', 'Read'), ValueError, 'duplicate names'),
    ],
  )
  def test_block_rejects_invalid_declarations(self, tool_names, error_type, message):
    with pytest.raises(error_type, match=message):
      mcp.block(*tool_names)

  def test_allow_commands_pairs_each_command_with_its_tool(self):
    layer = mcp.allow_commands('Monitor', 'watch one', 'watch two')
    assert layer.native_tool_commands == (('Monitor', 'watch one'), ('Monitor', 'watch two'))
    assert layer.blocked_native_tool_names == ()

  @pytest.mark.parametrize(
    ('commands', 'error_type', 'message'),
    [
      ((), ValueError, 'needs at least one command'),
      (('',), TypeError, 'non-empty \\(name, command\\) pairs'),
    ],
  )
  def test_allow_commands_rejects_invalid_declarations(self, commands, error_type, message):
    with pytest.raises(error_type, match=message):
      mcp.allow_commands('Monitor', *commands)

  @pytest.mark.parametrize(
    ('tool_names', 'error_type', 'message'),
    [
      ((), ValueError, 'needs at least one tool name'),
      (('',), TypeError, 'non-empty strings'),
      (('TaskStop', 'TaskStop'), ValueError, 'duplicate names'),
    ],
  )
  def test_serve_rejects_invalid_declarations(self, tool_names, error_type, message):
    with pytest.raises(error_type, match=message):
      mcp.serve(*tool_names)

  def test_layers_merge_into_one(self):
    layer = mcp.allow_commands('Monitor', 'watch it') | mcp.serve('TaskStop')
    assert layer.native_tool_commands == (('Monitor', 'watch it'),)
    assert layer.served_native_tool_names == ('TaskStop',)

  def test_mount_selects_from_one_toolset_type(self):
    toolset = mcp.Toolset('layer')

    @toolset.tool('read')
    def read() -> str:
      return 'read'

    full = mcp.mount(toolset)
    selected = mcp.mount(toolset, 'read')

    assert isinstance(full, mcp.ToolLayer)
    assert isinstance(selected, mcp.ToolLayer)
    assert len(full.server_specs) == 1
    assert len(selected.server_specs) == 1


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
    entries = ['plain', when(mcp.harness == 'bro', 'devtools')]
    assert select(entries, harness='bro') == ['plain', 'devtools']
    assert select(entries, harness='claude') == ['plain']

  def test_creds_fact_probes_availability(self, monkeypatch):
    monkeypatch.setattr(mcp.credentials, 'available', lambda name: name == 'openai')
    entries = [when(mcp.creds.contains('openai'), 'summary')]
    assert select(entries, creds=['openai']) == ['summary']
    monkeypatch.setattr(mcp.credentials, 'available', lambda name: False)
    assert select(entries, creds=['openai']) == []

  def test_absent_fact_raises_on_reference(self):
    with pytest.raises(ConditionError, match='unknown variable #wire'):
      select([when(mcp.wire == 'bare', 'x')], harness='bro')

  def test_unknown_harness_argument_raises(self):
    with pytest.raises(ValueError, match='unknown harness'):
      select([], harness='gemini')  # type: ignore[arg-type]


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
