import json

import pytest

import cw


@pytest.fixture
def config_path(monkeypatch, tmp_path):
  path = tmp_path / 'anthropic.json'
  monkeypatch.setattr(cw, '_ANTHROPIC_CONFIG_PATH', path)
  return path


class TestLoadAnthropicKey:
  def test_reads_from_config(self, config_path):
    config_path.write_text(json.dumps({'api_key': 'sk-from-file'}))
    assert cw._load_anthropic_key() == 'sk-from-file'

  def test_none_when_missing(self, config_path):
    assert cw._load_anthropic_key() is None

  def test_none_when_empty_value(self, config_path):
    config_path.write_text(json.dumps({'api_key': ''}))
    assert cw._load_anthropic_key() is None

  def test_none_when_field_missing(self, config_path):
    config_path.write_text(json.dumps({'something_else': 'x'}))
    assert cw._load_anthropic_key() is None


class TestBroClaudeArgv:
  def test_basic_shape(self):
    argv = cw._bro_claude_argv('pm')
    assert '--bare' in argv
    assert '--strict-mcp-config' in argv
    # slash commands must stay enabled so the bro's skills (populated as
    # .claude/skills/<name>/SKILL.md symlinks by the entrypoint) are reachable
    assert '--disable-slash-commands' not in argv
    # tools disabled (empty string follows --tools)
    i = argv.index('--tools')
    assert argv[i + 1] == ''
    # allowlist scoped to bro:
    i = argv.index('--allowed-tools')
    assert argv[i + 1] == 'mcp__bro__*'

  def test_mcp_config_points_at_shim(self):
    argv = cw._bro_claude_argv('pm')
    i = argv.index('--mcp-config')
    cfg = json.loads(argv[i + 1])
    bro_server = cfg['mcpServers']['bro']
    assert bro_server['command'] == 'mcp-server'
    assert bro_server['args'] == ['bro:pm']

  def test_system_prompt_is_bros_own(self):
    from bro.registry import create_bro

    bro = create_bro('pm')
    argv = cw._bro_claude_argv('pm')
    i = argv.index('--system-prompt')
    assert argv[i + 1] == bro.system_prompt

  def test_unknown_bro_raises(self):
    with pytest.raises(KeyError, match='unknown bro'):
      cw._bro_claude_argv('does-not-exist')


class TestSplitLaunchPrompt:
  def test_new_marker(self):
    head, prompt = cw._split_launch_prompt('dive-in --auto --new I want X')
    assert head == 'dive-in --auto --new '
    assert prompt == 'I want X'

  def test_dashdash_marker(self):
    head, prompt = cw._split_launch_prompt('cw ss name -- run the thing')
    assert head == 'cw ss name -- '
    assert prompt == 'run the thing'

  def test_p_marker(self):
    head, prompt = cw._split_launch_prompt('cw ss -p hello world')
    assert head == 'cw ss -p '
    assert prompt == 'hello world'

  def test_long_prompt_marker(self):
    head, prompt = cw._split_launch_prompt('cw ss --prompt do the thing')
    assert head == 'cw ss --prompt '
    assert prompt == 'do the thing'

  def test_no_marker_returns_command_unchanged(self):
    head, prompt = cw._split_launch_prompt('dive-in -t abc123')
    assert head == 'dive-in -t abc123'
    assert prompt is None

  def test_marker_without_trailing_content_is_not_a_match(self):
    # `dive-in --new ` with no seed should not produce an empty prompt
    head, prompt = cw._split_launch_prompt('dive-in --auto --new ')
    assert prompt is None
    assert head == 'dive-in --auto --new '


class TestSessionFacts:
  @pytest.fixture(autouse=True)
  def isolate_env(self, monkeypatch):
    # session-related env vars are picked up directly — wipe to a known state,
    # and stub the /.dockerenv probe so host runs don't accidentally read True
    for v in ('CW_NAME', 'CW_BRO', 'CW_COMMAND', 'PPP_SHELL_COMMAND', 'CW_HOST_WORKSPACE'):
      monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(cw, '_in_container', lambda: False)

  def test_container_session(self, monkeypatch):
    monkeypatch.setattr(cw, '_in_container', lambda: True)
    monkeypatch.setenv('CW_NAME', 'my-task')
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    monkeypatch.setenv('CW_HOST_WORKSPACE', '/host/var/cw/containers/my-task')
    monkeypatch.setenv('PPP_SHELL_COMMAND', 'dive-in -t abc')
    monkeypatch.setenv('CW_COMMAND', 'cw ss -c --auto --mcp my-task')
    facts = cw._session_facts()
    assert facts['in_container'] is True
    assert facts['name'] == 'my-task'
    assert facts['bro'] == 'ppp-dev'
    assert facts['host_workspace'] == '/host/var/cw/containers/my-task'
    assert facts['container_workspace'] == '/workspace'
    assert facts['exec_command'] == 'cw exec my-task'
    assert facts['shell_command'] == 'dive-in -t abc'
    assert facts['cw_command'] == 'cw ss -c --auto --mcp my-task'
    assert facts['prompt'] is None

  def test_extracts_prompt_from_dive_in_new(self, monkeypatch):
    monkeypatch.setenv('PPP_SHELL_COMMAND', 'dive-in --auto --new I want X')
    facts = cw._session_facts()
    assert facts['shell_command'] == 'dive-in --auto --new '
    assert facts['prompt'] == 'I want X'

  def test_host_worktree_with_derived_path(self, monkeypatch, tmp_path):
    proj = tmp_path / 'proj'
    worktree = proj / '.claude' / 'worktrees' / 'feature'
    worktree.mkdir(parents=True)
    monkeypatch.setattr(cw, '_project_root', lambda: proj)
    monkeypatch.setenv('CW_NAME', 'feature')
    monkeypatch.setenv('CW_COMMAND', 'cw ss feature')
    facts = cw._session_facts()
    assert facts['in_container'] is False
    assert facts['name'] == 'feature'
    assert facts['bro'] is None
    assert facts['host_workspace'] == str(worktree)
    assert facts['container_workspace'] is None
    assert facts['exec_command'] is None
    # PPP_SHELL_COMMAND defaults to CW_COMMAND — they're equal in this case
    assert facts['shell_command'] == 'cw ss feature'
    assert facts['cw_command'] == 'cw ss feature'

  def test_shell_command_falls_back_to_cw_command(self, monkeypatch):
    monkeypatch.setenv('CW_COMMAND', 'cw ss x')
    facts = cw._session_facts()
    assert facts['shell_command'] == 'cw ss x'
    assert facts['cw_command'] == 'cw ss x'

  def test_no_session_context(self):
    facts = cw._session_facts()
    assert facts['in_container'] is False
    assert facts['name'] is None
    assert facts['bro'] is None
    assert facts['host_workspace'] is None
    assert facts['exec_command'] is None
    assert facts['shell_command'] is None
    assert facts['cw_command'] is None
    assert facts['prompt'] is None


def _facts(**overrides) -> dict:
  base = {
    'in_container': True,
    'name': 'task',
    'bro': None,
    'host_workspace': '/h/ws',
    'container_workspace': '/workspace',
    'exec_command': 'cw exec task',
    'cw_command': None,
    'shell_command': None,
    'prompt': None,
  }
  base.update(overrides)
  return base


class TestRenderBanner:
  def test_llm_emits_plain_key_value(self):
    out = cw._render_banner_llm(
      _facts(
        bro='ppp-dev',
        cw_command='cw ss -c --mcp task',
        shell_command='dive-in -t x',
      )
    )
    assert '\033[' not in out  # no ANSI
    assert '██' not in out  # no logo
    assert 'kind: container' in out
    assert 'name: task' in out
    assert 'bro: ppp-dev' in out
    assert 'workspace_host_path: /h/ws' in out
    assert 'workspace_container_path: /workspace' in out
    assert 'docker_shell_command: cw exec task' in out
    assert 'cw_command: cw ss -c --mcp task' in out
    assert 'launch_command: dive-in -t x' in out

  def test_llm_excludes_prompt_to_save_context(self):
    # the LLM already has the prompt as its first message — re-emitting it
    # would just burn tokens. launch_command keeps the trailing marker as the
    # signal that a seed prompt exists, but the body itself is omitted.
    out = cw._render_banner_llm(_facts(shell_command='dive-in --new ', prompt='I want X'))
    assert 'I want X' not in out
    assert 'prompt:' not in out
    assert 'launch_command: dive-in --new' in out

  def test_llm_suppresses_cw_command_when_equal_to_shell_command(self):
    out = cw._render_banner_llm(_facts(cw_command='cw ss feature', shell_command='cw ss feature'))
    assert 'cw_command:' not in out
    assert 'launch_command: cw ss feature' in out

  def test_llm_omits_none_fields(self):
    out = cw._render_banner_llm(
      _facts(
        in_container=False,
        name=None,
        host_workspace=None,
        container_workspace=None,
        exec_command=None,
      )
    )
    assert out == 'kind: host worktree'

  def test_visual_shows_logo_with_bro_signature(self):
    out = cw._render_banner_visual(_facts(bro='pm'))
    # logo present (top five lines unchanged); bottom line gets a `// <bro>`
    # signature — dim slashes, bright-white-bold bro name
    for line in cw._BRO_LOGO.split('\n')[:-1]:
      assert line in out
    assert '\033[2m//\033[0m \033[1;97mpm\033[0m' in out
    # no parens-form kind on the session line — encoded by the c: prefix instead
    assert '(container)' not in out
    assert '(host worktree)' not in out

  def test_visual_session_line_uses_c_prefix_for_container(self):
    out = cw._render_banner_visual(_facts(name='task'))
    assert 'cw session:   \033[1mc:task\033[0m' in out

  def test_visual_container_shows_workspace_and_host_path_on_separate_lines(self):
    out = cw._render_banner_visual(_facts(host_workspace='/host/var/cw/containers/task'))
    assert 'workspace:    /workspace' in out
    assert 'host path:    \033[2m/host/var/cw/containers/task\033[0m' in out

  def test_visual_uses_docker_shell_label(self):
    out = cw._render_banner_visual(_facts())
    assert 'docker shell:' in out
    assert 'host shell:' not in out

  def test_visual_session_line_omits_prefix_for_worktree(self):
    out = cw._render_banner_visual(
      _facts(
        in_container=False,
        name='feature',
        host_workspace='/proj/.claude/worktrees/feature',
        container_workspace=None,
        exec_command=None,
      )
    )
    # in worktree mode there's no `docker shell:` row, so the widest label is
    # `cw session:` (11 chars) — value follows with one space, no extra padding
    assert 'cw session: \033[1mfeature\033[0m' in out
    assert 'c:feature' not in out

  def test_visual_skips_logo_for_non_bro(self):
    out = cw._render_banner_visual(_facts())
    assert '██' not in out

  def test_visual_paints_host_path_red_for_worktree(self):
    out = cw._render_banner_visual(
      _facts(
        in_container=False,
        name='feature',
        host_workspace='/proj/.claude/worktrees/feature',
        container_workspace=None,
        exec_command=None,
      )
    )
    assert '\033[31m/proj/.claude/worktrees/feature\033[0m' in out

  def test_visual_does_not_paint_container_path_red(self):
    out = cw._render_banner_visual(_facts(host_workspace='/host/var/cw/containers/task'))
    assert '\033[31m' not in out

  def test_visual_handles_missing_host_path_in_worktree(self):
    out = cw._render_banner_visual(
      _facts(
        in_container=False,
        name=None,
        host_workspace=None,
        container_workspace=None,
        exec_command=None,
      )
    )
    assert '(unknown' in out

  def test_visual_shows_cw_command_when_distinct(self):
    out = cw._render_banner_visual(
      _facts(cw_command='cw ss -c --mcp task', shell_command='dive-in -t x')
    )
    assert 'cw command:' in out
    assert 'cw ss -c --mcp task' in out

  def test_visual_suppresses_cw_command_when_equal(self):
    out = cw._render_banner_visual(
      _facts(cw_command='cw ss feature', shell_command='cw ss feature')
    )
    assert 'cw command:' not in out

  def test_visual_replaces_prompt_with_placeholder_and_separate_line(self):
    out = cw._render_banner_visual(_facts(shell_command='dive-in --new ', prompt='I want a banner'))
    # bright-white bold for both the @prompt@ placeholder and the prompt: label
    # (the label style wraps the padded label, so trailing spaces sit inside)
    assert '\033[1;97m@prompt@\033[0m' in out
    assert '\033[1;97mprompt:' in out and '\033[0m' in out
    # the actual prompt text appears once, on its own line
    assert 'I want a banner' in out
    assert out.count('I want a banner') == 1


class TestPopulateBroSkills:
  def test_creates_symlinks_for_each_skill(self, tmp_path):
    # ppp-dev inherits /pr and /land from dev via the MRO walk
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    skills_dir = tmp_path / '.claude' / 'skills'
    for name in ('pr', 'land'):
      link = skills_dir / name / 'SKILL.md'
      assert link.is_symlink()
      assert link.resolve().name == f'{name}.md'

  def test_symlinks_are_relative(self, tmp_path):
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    link = tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    target = link.readlink()
    assert not target.is_absolute()

  def test_wipes_stale_symlinks_before_recreating(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    stale = skills_dir / 'stale'
    stale.mkdir(parents=True)
    (stale / 'SKILL.md').symlink_to('/nonexistent')
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    assert not stale.exists()

  def test_leaves_static_skills_alone(self, tmp_path):
    skills_dir = tmp_path / '.claude' / 'skills'
    static = skills_dir / 'deploy'
    static.mkdir(parents=True)
    skill_md = static / 'SKILL.md'
    skill_md.write_text('static content')
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    assert skill_md.is_file()
    assert not skill_md.is_symlink()
    assert skill_md.read_text() == 'static content'

  def test_creates_skills_dir_if_missing(self, tmp_path):
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    assert (tmp_path / '.claude' / 'skills').is_dir()

  def test_idempotent(self, tmp_path):
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    cw._populate_bro_skills(tmp_path, 'ppp-dev')
    link = tmp_path / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    assert link.is_symlink()
