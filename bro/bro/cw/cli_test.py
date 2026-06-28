import json
import os
from typing import Optional
from unittest.mock import patch

import pytest

import cw


@pytest.fixture
def config_path(monkeypatch, tmp_path):
  from base import credentials

  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, 'PPP_DIR', str(tmp_path))
  monkeypatch.setattr(credentials, '_default_store', None)
  return tmp_path / 'anthropic.json'


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


class TestClaudeCodeTokenEnv:
  def test_present_exports_token(self, config_path):
    # the `claude_code` secret is a scalar token file; the store strips it.
    (config_path.parent / 'claude_code_oauth_token').write_text('oauth-tok\n')
    assert cw._claude_code_token_env() == {'CLAUDE_CODE_OAUTH_TOKEN': 'oauth-tok'}

  def test_absent_yields_empty(self, config_path):
    # not populated → no overlay, host session falls back to ~/.claude/.credentials.json
    assert cw._claude_code_token_env() == {}


class TestSeedContainerClaudeJSON:
  def _host(self, tmp_path, **extra):
    path = tmp_path / 'host.json'
    path.write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'uid', **extra}))
    return path

  def _seed_dir(self, tmp_path):
    d = tmp_path / 'seed'
    d.mkdir()
    return d

  def test_constructs_explicit_config_plus_identity(self, tmp_path):
    host = self._host(tmp_path, projects={'/x': {}}, numStartups=42)
    seed = cw._seed_container_claude_json(self._seed_dir(tmp_path), host)
    data = json.loads(seed.read_text())
    assert data['installMethod'] == 'global'
    assert data['hasCompletedOnboarding'] is True
    assert data['projects']['/workspace']['hasTrustDialogAccepted'] is True
    assert data['oauthAccount'] == {'id': 'acct'}
    assert data['userID'] == 'uid'
    # host machine state must not leak in
    assert '/x' not in data['projects']
    assert 'numStartups' not in data

  def test_missing_host_file_is_fatal(self, tmp_path):
    with pytest.raises(SystemExit):
      cw._seed_container_claude_json(self._seed_dir(tmp_path), tmp_path / 'absent.json')

  def test_missing_identity_key_is_fatal(self, tmp_path):
    host = tmp_path / 'host.json'
    host.write_text(json.dumps({'userID': 'uid'}))
    with pytest.raises(SystemExit):
      cw._seed_container_claude_json(self._seed_dir(tmp_path), host)

  def test_seed_is_not_overwritten_on_second_call(self, tmp_path):
    seed_dir = self._seed_dir(tmp_path)
    seed = cw._seed_container_claude_json(seed_dir, self._host(tmp_path))
    seed.write_text(json.dumps({'container': 'wrote-this'}))
    again = cw._seed_container_claude_json(seed_dir, self._host(tmp_path))
    assert json.loads(again.read_text()) == {'container': 'wrote-this'}


class TestPluginSeedContract:
  # the enabled plugin must also be installed: settings.json enables it (cw.py),
  # the Dockerfile installs + stages it, and the entrypoint copies the stage into
  # the bind-mounted ~/.claude/plugins. enabling without installing is exactly the
  # regression that reintroduced the "LSP Plugin Recommendation" prompt.
  _SEED_DIR = '/opt/claude-plugins-seed'

  def test_settings_enables_pyright_lsp(self):
    assert cw._CONTAINER_SETTINGS_JSON['enabledPlugins'] == {
      'pyright-lsp@claude-plugins-official': True
    }

  def test_claude_json_suppresses_marketplace_autoinstall(self):
    # the marketplace is baked into the image, so the runtime auto-install (a
    # network fetch that can also prompt) must be marked already-done.
    assert cw._CONTAINER_CLAUDE_JSON['officialMarketplaceAutoInstallAttempted'] is True

  def test_dockerfile_installs_and_stages_the_enabled_plugin(self):
    plugin = next(iter(cw._CONTAINER_SETTINGS_JSON['enabledPlugins']))
    dockerfile = (cw.CONTAINER_DIR / 'Dockerfile').read_text()
    assert f'claude plugin install {plugin}' in dockerfile
    assert self._SEED_DIR in dockerfile

  def test_entrypoint_copies_the_stage(self):
    entrypoint = (cw.CONTAINER_DIR / 'entrypoint.sh').read_text()
    assert self._SEED_DIR in entrypoint
    assert '.claude/plugins' in entrypoint


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


class TestSessionAppendPrompt:
  def test_includes_base_prompts(self):
    out = cw._session_append_prompt(False, None)
    assert 'Interaction policy' in out
    assert 'Land mode: PR' not in out

  def test_auto_adds_land_mode(self):
    assert 'Land mode: PR' in cw._session_append_prompt(True, None)

  def test_bro_persona_injected(self):
    out = cw._session_append_prompt(False, 'ppp-dev')
    assert '## PPP project' in out
    assert "wasn't in the room" in out

  def test_no_persona_without_bro(self):
    assert '## PPP project' not in cw._session_append_prompt(False, None)


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
    worktree = proj / 'var' / 'cw' / 'worktrees' / 'feature'
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

  def test_bro_override_takes_precedence_over_env(self, monkeypatch):
    # `call` passes its bro explicitly even though CW_BRO isn't forwarded into a
    # call container; the override wins over whatever (if anything) is in the env
    monkeypatch.delenv('CW_BRO', raising=False)
    assert cw._session_facts(bro_override='librorian')['bro'] == 'librorian'
    monkeypatch.setenv('CW_BRO', 'pm')
    assert cw._session_facts(bro_override='librorian')['bro'] == 'librorian'
    assert cw._session_facts()['bro'] == 'pm'


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
    'sync_warning': None,
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
        host_workspace='/proj/var/cw/worktrees/feature',
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
        host_workspace='/proj/var/cw/worktrees/feature',
        container_workspace=None,
        exec_command=None,
      )
    )
    assert '\033[31m/proj/var/cw/worktrees/feature\033[0m' in out

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

  def test_llm_emits_sync_warning_as_first_line(self):
    out = cw._render_banner_llm(
      _facts(sync_warning='session-log sync FAILING — run setup/bootstrap_session_log.sh')
    )
    # first line so it survives Claude's collapsed tool-output preview
    assert out.splitlines()[0] == 'session_log_sync: FAILING — run setup/bootstrap_session_log.sh'
    assert 'kind: container' in out

  def test_llm_omits_sync_warning_when_healthy(self):
    out = cw._render_banner_llm(_facts())
    assert 'session_log_sync' not in out

  def test_visual_paints_sync_warning_red_above_logo(self):
    out = cw._render_banner_visual(
      _facts(bro='pm', sync_warning='session-log sync FAILING — run setup/bootstrap_session_log.sh')
    )
    first = out.splitlines()[0]
    assert first.startswith('\033[31m\033[1m⚠ ')
    assert 'session-log sync FAILING' in first
    # warning sits above the bro logo
    assert out.index('⚠') < out.index('██')


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestCleanupImage:
  def test_prefers_current_tag_when_present(self, monkeypatch):
    monkeypatch.setattr(cw, '_image_tag', lambda: 'ppp-cw:cur')
    monkeypatch.setattr(cw.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    assert cw._cleanup_image() == 'ppp-cw:cur'

  def test_falls_back_to_any_ppp_cw_image(self, monkeypatch):
    monkeypatch.setattr(cw, '_image_tag', lambda: 'ppp-cw:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':  # docker image inspect -> miss
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='ppp-cw:<none>\nppp-cw:abc123\n')

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    assert cw._cleanup_image() == 'ppp-cw:abc123'

  def test_none_when_no_image(self, monkeypatch):
    monkeypatch.setattr(cw, '_image_tag', lambda: 'ppp-cw:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='')

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    assert cw._cleanup_image() is None


class TestRemoveContainerDir:
  def test_plain_rmtree_when_host_owned(self, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cw.shutil, 'rmtree', lambda p: calls.append(p))
    monkeypatch.setattr(
      cw.subprocess, 'run', lambda *a, **k: pytest.fail('docker must not be invoked')
    )
    cw._remove_container_dir(tmp_path / 'ws', image='ppp-cw:x')
    assert calls == [tmp_path / 'ws']

  def test_missing_dir_is_noop(self, monkeypatch, tmp_path):
    def boom(_):
      raise FileNotFoundError

    monkeypatch.setattr(cw.shutil, 'rmtree', boom)
    monkeypatch.setattr(
      cw.subprocess, 'run', lambda *a, **k: pytest.fail('docker must not be invoked')
    )
    cw._remove_container_dir(tmp_path / 'gone', image='ppp-cw:x')

  def test_escalates_to_root_container_on_eperm(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(cw.shutil, 'rmtree', boom)
    seen = {}

    def fake_run(argv, *a, **k):
      seen['argv'] = argv
      return _FakeProc(returncode=0)

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    target = tmp_path / 'ws'  # never created -> path.exists() is False afterwards
    cw._remove_container_dir(target, image='ppp-cw:x')
    argv = seen['argv']
    assert argv[:5] == ['docker', 'run', '--rm', '-u', '0']
    assert '--entrypoint' in argv and argv[argv.index('--entrypoint') + 1] == 'rm'
    assert f'{tmp_path}:/target' in argv
    assert argv[-2:] == ['-rf', f'/target/{target.name}']

  def test_raises_when_no_image_available(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(cw.shutil, 'rmtree', boom)
    with pytest.raises(RuntimeError, match='no ppp-cw image'):
      cw._remove_container_dir(tmp_path / 'ws', image=None)

  def test_raises_when_docker_rm_fails(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(cw.shutil, 'rmtree', boom)
    monkeypatch.setattr(
      cw.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=1, stderr='denied')
    )
    with pytest.raises(RuntimeError, match='docker rm failed: denied'):
      cw._remove_container_dir(tmp_path / 'ws', image='ppp-cw:x')

  def test_raises_when_dir_survives_docker_rm(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(cw.shutil, 'rmtree', boom)
    monkeypatch.setattr(cw.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    survivor = tmp_path / 'ws'
    survivor.mkdir()  # still present after the mocked docker rm
    with pytest.raises(RuntimeError, match='still present'):
      cw._remove_container_dir(survivor, image='ppp-cw:x')


class TestWorktreeIsClean:
  def _git(self, cwd, *args):
    import subprocess

    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)

  def _repo(self, tmp_path):
    d = tmp_path / 'repo'
    d.mkdir()
    self._git(d, 'init', '-b', 'master')
    self._git(d, 'config', 'user.email', 't@t')
    self._git(d, 'config', 'user.name', 't')
    (d / 'f').write_text('a')
    self._git(d, 'add', '.')
    self._git(d, 'commit', '-m', 'c1')
    # simulate a pushed upstream at the current commit
    self._git(d, 'update-ref', 'refs/remotes/origin/master', 'HEAD')
    return d

  def test_clean_when_head_matches_origin_master(self, tmp_path):
    d = self._repo(tmp_path)
    safe, reasons = cw._worktree_is_clean(d, refresh_origin=False)
    assert safe is True
    assert reasons == []

  def test_counts_unpushed_commits(self, tmp_path):
    d = self._repo(tmp_path)
    (d / 'f').write_text('b')
    self._git(d, 'commit', '-am', 'c2')
    safe, reasons = cw._worktree_is_clean(d, refresh_origin=False)
    assert safe is False
    assert reasons == ['1 commit(s) not on origin/master']

  def test_flags_uncommitted_changes(self, tmp_path):
    d = self._repo(tmp_path)
    (d / 'untracked').write_text('x')
    safe, reasons = cw._worktree_is_clean(d, refresh_origin=False)
    assert safe is False
    assert 'uncommitted or untracked changes' in reasons

  def test_missing_origin_master_is_not_clean(self, tmp_path):
    d = tmp_path / 'repo'
    d.mkdir()
    self._git(d, 'init', '-b', 'master')
    self._git(d, 'config', 'user.email', 't@t')
    self._git(d, 'config', 'user.name', 't')
    (d / 'f').write_text('a')
    self._git(d, 'add', '.')
    self._git(d, 'commit', '-m', 'c1')
    safe, reasons = cw._worktree_is_clean(d, refresh_origin=False)
    assert safe is False
    assert 'origin/master not found' in reasons


class TestIsLocalActive:
  def _seed(self, monkeypatch, tmp_path, name):
    monkeypatch.setattr(cw, '_project_root', lambda: tmp_path)
    pidfile = cw._host_pidfile(tmp_path, name)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    return pidfile

  def test_false_when_no_pidfile(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw, '_project_root', lambda: tmp_path)
    assert cw._is_local_active('feat') is False

  def test_true_for_live_pid(self, monkeypatch, tmp_path):
    import os

    self._seed(monkeypatch, tmp_path, 'feat').write_text(str(os.getpid()))
    assert cw._is_local_active('feat') is True

  def test_false_for_dead_pid(self, monkeypatch, tmp_path):
    import subprocess

    proc = subprocess.Popen(['true'])
    proc.wait()
    self._seed(monkeypatch, tmp_path, 'feat').write_text(str(proc.pid))
    assert cw._is_local_active('feat') is False

  def test_false_for_garbage(self, monkeypatch, tmp_path):
    self._seed(monkeypatch, tmp_path, 'feat').write_text('not-a-pid')
    assert cw._is_local_active('feat') is False


class TestFinishHostWorktree:
  def _patch(self, monkeypatch, *, clean):
    reasons = [] if clean else ['1 commit(s) not on origin/master']
    monkeypatch.setattr(cw, '_worktree_is_clean', lambda p: (clean, reasons))
    removed: list = []
    monkeypatch.setattr(cw, '_remove_host_worktree', lambda wt, br: removed.append((wt, br)))
    return removed

  def test_interactive_drops_on_yes(self, monkeypatch, tmp_path):
    removed = self._patch(monkeypatch, clean=True)
    monkeypatch.setattr(cw, 'yesno', lambda q: True)
    cw._finish_host_worktree('feat', tmp_path / 'wt', 'worktree-feat', interactive=True)
    assert removed == [(tmp_path / 'wt', 'worktree-feat')]

  def test_interactive_keeps_on_no(self, monkeypatch, tmp_path):
    removed = self._patch(monkeypatch, clean=True)
    monkeypatch.setattr(cw, 'yesno', lambda q: False)
    cw._finish_host_worktree('feat', tmp_path / 'wt', 'worktree-feat', interactive=True)
    assert removed == []

  def test_non_interactive_keeps_and_never_prompts(self, monkeypatch, tmp_path):
    removed = self._patch(monkeypatch, clean=False)

    def boom(q):
      raise AssertionError('must not prompt in a non-interactive session')

    monkeypatch.setattr(cw, 'yesno', boom)
    cw._finish_host_worktree('feat', tmp_path / 'wt', 'worktree-feat', interactive=False)
    assert removed == []


class TestEnsureHostWorktree:
  def _recorder(self, monkeypatch, *, branch_exists=False):
    from types import SimpleNamespace

    calls: list = []

    def fake_run(args, **kwargs):
      calls.append(args)
      is_show_ref = len(args) > 1 and args[1] == 'show-ref'
      rc = 0 if (branch_exists or not is_show_ref) else 1
      return SimpleNamespace(returncode=rc, stdout='')

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    return calls

  def _add_cmd(self, calls):
    return next(c for c in calls if c[:3] == ['git', 'worktree', 'add'])

  def test_new_branch_uses_base_ref(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    wt = tmp_path / 'wt'
    assert cw._ensure_host_worktree(wt, 'worktree-x', 'sha123') is True
    assert self._add_cmd(calls) == ['git', 'worktree', 'add', str(wt), '-b', 'worktree-x', 'sha123']

  def test_new_branch_defaults_to_head(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    wt = tmp_path / 'wt'
    assert cw._ensure_host_worktree(wt, 'worktree-x') is True
    assert self._add_cmd(calls) == ['git', 'worktree', 'add', str(wt), '-b', 'worktree-x']

  def test_existing_branch_ignores_base_ref(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch, branch_exists=True)
    wt = tmp_path / 'wt'
    assert cw._ensure_host_worktree(wt, 'worktree-x', 'sha123') is True
    assert self._add_cmd(calls) == ['git', 'worktree', 'add', str(wt), 'worktree-x']

  def test_existing_dir_is_noop(self, monkeypatch, tmp_path):
    calls = self._recorder(monkeypatch)
    wt = tmp_path / 'wt'
    wt.mkdir()
    assert cw._ensure_host_worktree(wt, 'worktree-x', 'sha123') is True
    assert calls == []


class TestResolveBaseRef:
  def _patch(self, monkeypatch, *, local_rc, fetch_rc=1, fetched_sha='deadbeef'):
    import pathlib
    from types import SimpleNamespace

    monkeypatch.setattr(cw, '_project_root', lambda: pathlib.Path('/repo'))
    calls: list = []

    def fake_run(args, **kwargs):
      calls.append(args)
      if args[:3] == ['git', 'rev-parse', '--verify'] and args[3] == 'FETCH_HEAD^{commit}':
        return SimpleNamespace(returncode=0, stdout=f'{fetched_sha}\n')
      if args[:3] == ['git', 'rev-parse', '--verify']:
        return SimpleNamespace(returncode=local_rc, stdout='localsha\n' if local_rc == 0 else '')
      if args[:2] == ['git', 'fetch']:
        return SimpleNamespace(returncode=fetch_rc, stdout='')
      raise AssertionError(f'unexpected command {args}')

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    return calls

  def test_resolves_host_local_ref_without_fetching(self, monkeypatch):
    calls = self._patch(monkeypatch, local_rc=0)
    assert cw._resolve_base_ref('master') == 'localsha'
    assert not any(c[:2] == ['git', 'fetch'] for c in calls)

  def test_fetches_origin_when_ref_not_host_local(self, monkeypatch):
    calls = self._patch(monkeypatch, local_rc=1, fetch_rc=0, fetched_sha='abc123')
    assert cw._resolve_base_ref('worktree-feature') == 'abc123'
    assert ['git', 'fetch', 'origin', 'worktree-feature'] in calls

  def test_returns_none_when_neither_resolves(self, monkeypatch):
    self._patch(monkeypatch, local_rc=1, fetch_rc=1)
    assert cw._resolve_base_ref('nope') is None


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

  def test_relative_symlinks_resolve_through_var_style_symlink(self, tmp_path):
    # macOS tempfile.mkdtemp() returns /var/folders/… where /var → /private/var,
    # so the symlink target sits one dir deeper than its logical path. mirror that
    # with a `var` → `private/var` indirection: a relpath against the logical path
    # would be off by one level and the skill symlink would dangle.
    real = tmp_path / 'private' / 'var'
    real.mkdir(parents=True)
    proj = tmp_path / 'var'
    proj.symlink_to(real)
    cw._populate_bro_skills(proj, 'ppp-dev')
    link = proj / '.claude' / 'skills' / 'pr' / 'SKILL.md'
    assert link.is_symlink()
    assert not link.readlink().is_absolute()
    assert os.path.exists(link)
    assert len(link.read_text()) > 0

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


class TestContainerSecrets:
  def test_claude_code_session_set(self):
    # native claude code session themed as ppp-dev: baseline + the bro's
    # extra_secrets (github) + the deployed flow MCP token (--mcp http) + the
    # claude_code OAuth token (the session's only auth). NOT the bro's in-process
    # MCP secret (notion) — claude code uses flow_mcp instead.
    secrets, optional, docker_sock = cw._container_secrets('ppp-dev', mcp='http', bro_mode=False)
    assert {'session_log', 'trails', 'github', 'flow_mcp', 'claude_code'} <= secrets
    assert 'notion' not in secrets
    # required (strict), not optional: no .credentials.json fallback in the
    # container, so a missing token must fail loudly on the host
    assert optional == set()
    # a normal claude code session keeps the docker socket
    assert docker_sock is True

  def test_no_flow_mcp_without_http(self):
    secrets, _, _ = cw._container_secrets('ppp-dev', mcp=None, bro_mode=False)
    assert 'flow_mcp' not in secrets

  def test_bro_mode_uses_full_manifest_and_anthropic(self):
    # --bro serves the bro's own MCP servers, so it gets the full manifest (notion)
    # plus anthropic for the apiKeyHelper. ppp-dev doesn't deploy → no docker socket.
    secrets, optional, docker_sock = cw._container_secrets('ppp-dev', mcp=None, bro_mode=True)
    assert {'notion', 'github', 'anthropic'} <= secrets
    assert docker_sock is False
    # --bro runs claude --bare, which ignores CLAUDE_CODE_OAUTH_TOKEN, so the token
    # secret is not requested on this surface
    assert 'claude_code' not in optional
    assert 'claude_code' not in secrets

  def test_bro_mode_includes_optional_secrets(self):
    # a bro with searchable data sources (librorian) advertises openai best-effort
    # for the query-focused fetch summary; --bro hydrates it as the optional tier.
    secrets, optional, _ = cw._container_secrets('librorian', mcp=None, bro_mode=True)
    assert 'openai' in optional
    assert 'openai' not in secrets  # optional, not required

  def test_docker_socket_only_for_deploy_bros(self):
    # the socket is gated on needs_docker: devoops (deployer) keeps it, librorian doesn't
    _, _, devoops_sock = cw._container_secrets('devoops', mcp=None, bro_mode=True)
    secrets, _, librorian_sock = cw._container_secrets('librorian', mcp=None, bro_mode=True)
    assert devoops_sock is True
    assert librorian_sock is False
    assert {'tmdb', 'brave', 'notion'} <= secrets

  def test_unknown_bro_falls_back_to_baseline(self):
    secrets, optional, docker_sock = cw._container_secrets(
      'nonexistent-bro', mcp=None, bro_mode=False
    )
    assert secrets == set(cw._CW_SESSION_BASELINE)
    assert optional == set()
    assert docker_sock is True


class TestGrantRevoke:
  def test_extract_forwarded_argv_round_trips_grant_revoke(self):
    from base.args import Parser

    parser = Parser(add_help=False)
    cw.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--grant', 'a', '--grant', 'b', '--revoke', 'c']))
    assert cw.extract_forwarded_argv(args) == ['--grant', 'a', '--grant', 'b', '--revoke', 'c']

  def test_extract_forwarded_argv_round_trips_into(self):
    from base.args import Parser

    parser = Parser(add_help=False)
    cw.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--into', 'my-branch']))
    assert cw.extract_forwarded_argv(args) == ['--into', 'my-branch']

  def _start(
    self,
    *,
    grant: Optional[list[str]] = None,
    revoke: Optional[list[str]] = None,
    effort: Optional[str] = None,
  ) -> int:
    return cw.start_session(
      name='w',
      container=True,
      drop=True,
      auto=False,
      fast=False,
      grant=grant,
      revoke=revoke,
      effort=effort,
      rc=False,
      resume=False,
      into=None,
      mcp=None,
      bro=None,
      prompt=None,
      claude_args=[],
    )

  def test_start_session_applies_grant_and_revoke(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.cw', return_value=0) as fake_cw,
      patch('cw._container_secrets', return_value=({'notion', 'trails', 'github'}, set(), True)),
      patch('cw._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      rc = self._start(grant=['gmail_creds'], revoke=['notion'])
    assert rc == 0
    _, kwargs = fake_cw.call_args
    assert 'gmail_creds' in kwargs['secrets']
    assert 'notion' not in kwargs['secrets']

  def test_start_session_grant_already_present_returns_1(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.cw', return_value=0) as fake_cw,
      patch('cw._container_secrets', return_value=({'github'}, set(), True)),
      patch('cw._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      rc = self._start(grant=['github'])
    assert rc == 1
    assert fake_cw.call_count == 0

  def test_ss_grant_without_container_errors(self, capsys):
    with pytest.raises(SystemExit):
      cw.main(['cw', 'ss', '--grant', 'gmail_creds', 'wsname'])
    assert 'require -c' in capsys.readouterr().err

  def test_effort_defaults_to_xhigh(self):
    from base.args import Parser

    parser = Parser(add_help=False)
    cw.add_forwarded_flags(parser)
    assert parser.parse_args([]).effort == 'xhigh'

  def test_start_session_injects_effort_into_claude_args(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.cw', return_value=0) as fake_cw,
      patch('cw._container_secrets', return_value=({'github'}, set(), True)),
      patch('cw._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      rc = self._start(effort='xhigh')
    assert rc == 0
    _, kwargs = fake_cw.call_args
    claude_args = kwargs['claude_args']
    idx = claude_args.index('--effort')
    assert claude_args[idx + 1] == 'xhigh'


class TestResumeCommand:
  def test_create_command_includes_drop_into_and_claude_args(self):
    parts = cw._cw_session_command(
      'w',
      container=True,
      auto=True,
      fast=True,
      drop=True,
      rc=True,
      resume=False,
      effort='xhigh',
      mcp='http',
      bro=None,
      grant=['gmail_creds'],
      revoke=['notion'],
      into='feature',
      claude_args=['--foo'],
    )
    assert parts == [
      'cw', 'ss', '-c', '--auto', '--fast', '--drop', '--rc',
      '--effort', 'xhigh', '--mcp', '--grant', 'gmail_creds',
      '--revoke', 'notion', '--into', 'feature', 'w', '--foo',
    ]  # fmt: skip

  def test_resume_command_carries_forwarded_flags(self):
    # the same session as a resume: keeps --auto/--rc/--effort/--mcp/--grant,
    # adds --resume, and drops the create-only --drop/--into/claude args
    parts = cw._cw_session_command(
      'w',
      container=True,
      auto=True,
      fast=False,
      drop=False,
      rc=True,
      resume=True,
      effort='xhigh',
      mcp='http',
      bro=None,
      grant=['gmail_creds'],
      revoke=[],
      into=None,
      claude_args=[],
    )
    assert parts == [
      'cw', 'ss', '-c', '--auto', '--rc', '--resume',
      '--effort', 'xhigh', '--mcp', '--grant', 'gmail_creds', 'w',
    ]  # fmt: skip

  def test_start_session_records_resume_command(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.cw', return_value=0),
      patch('cw._container_secrets', return_value=({'github'}, set(), True)),
      patch('cw._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      cw.start_session(
        name='w',
        container=True,
        drop=True,
        auto=True,
        fast=False,
        grant=['gmail_creds'],
        revoke=None,
        effort='xhigh',
        rc=False,
        resume=False,
        into=None,
        mcp='http',
        bro=None,
        prompt=None,
        claude_args=[],
      )
      resume_command = env['CW_RESUME_COMMAND']
    assert (
      resume_command == 'cw ss -c --auto --rc --resume --effort xhigh --mcp --grant gmail_creds w'
    )

  def test_replace_container_resume_hint_prints_recorded_command(self, monkeypatch, capsys):
    monkeypatch.setenv('CW_RESUME_COMMAND', 'cw ss -c --auto --resume w')
    monkeypatch.setattr(cw, '_projects_dir_for_container', lambda name: name)
    monkeypatch.setattr(cw, '_latest_jsonl', lambda d: 'session.jsonl')
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    cw._replace_container_resume_hint('w')
    assert 'cw ss -c --auto --resume w' in capsys.readouterr().out

  def test_bro_with_resume_is_accepted(self):
    with (
      patch('cw._load_anthropic_key', return_value={'api_key': 'k'}),
      patch('cw.start_session', return_value=0) as fake_start,
    ):
      rc = cw.main(['cw', 'ss', '-c', '--bro', 'ppp-dev', '--resume', 'w'])
    assert rc == 0
    assert fake_start.call_count == 1


class TestDockerCreateArgv:
  @pytest.fixture
  def build_argv(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(cw, '_seed_container_claude_json', lambda d, h: tmp_path / '.claude.json')

    def build(**kwargs):
      return cw._docker_create_argv(
        'tag', 'ws', tmp_path / 'proj', tmp_path / 'sess', ['claude'], **kwargs
      )

    return build

  def test_uses_docker_create_run_equivalent(self, build_argv):
    # `docker create -it --rm` is the unstarted half of `docker run -it --rm`;
    # run_in_container pairs it with `docker start -a -i`.
    assert build_argv()[:4] == ['docker', 'create', '-it', '--rm']

  def test_docker_sock_mounted_by_default(self, build_argv):
    assert '/var/run/docker.sock:/var/run/docker.sock' in build_argv()

  def test_docker_sock_dropped_when_disabled(self, build_argv):
    assert '/var/run/docker.sock:/var/run/docker.sock' not in build_argv(docker_sock=False)

  def test_base_ref_passed_as_env(self, build_argv):
    argv = build_argv(extra_env={'CW_BASE_REF': 'deadbeef'})
    assert 'CW_BASE_REF=deadbeef' in argv

  def test_no_ppp_mount(self, build_argv):
    # the scoped store is injected via `docker cp`, never bind-mounted
    assert not any('/home/cw/.ppp' in a for a in build_argv())

  def test_no_out_of_band_github_token_mount(self, build_argv):
    assert not any('/run/secrets/github_token' in a for a in build_argv())

  def test_ambient_github_token_not_forwarded(self, build_argv, monkeypatch):
    # an ambient host GITHUB_TOKEN must not leak into the container — github
    # arrives only via the scoped `github` secret's install hook.
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_leak')
    argv = build_argv()
    assert 'GITHUB_TOKEN' not in argv
    assert not any('ghp_leak' in a for a in argv)

  def test_term_forwarded_for_color_fidelity(self, build_argv, monkeypatch):
    # forward the host TERM so in-container TUIs detect the same color tier — docker
    # otherwise defaults TERM=xterm (low tier), flattening dim/256-color styling.
    monkeypatch.setenv('TERM', 'tmux-256color')
    argv = build_argv()
    assert 'TERM' in argv
    assert argv[argv.index('TERM') - 1] == '-e'

  def test_cw_bro_forwarded_by_default(self, build_argv, monkeypatch):
    # the Claude Code session path relies on CW_BRO reaching the container so the
    # entrypoint runs `cw populate-bro-skills`.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' in build_argv()

  def test_cw_bro_dropped_when_forward_bro_false(self, build_argv, monkeypatch):
    # the ask/do/call hop runs the bro as an LLM process (no Claude Code), so the
    # calling session's ambient CW_BRO must not leak in and trigger a skills populate.
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    assert 'CW_BRO' not in build_argv(forward_bro=False)

  def test_extra_env_injected_as_explicit_key_value(self, build_argv, monkeypatch):
    # extra_env sets the value here (`-e KEY=VALUE`), unlike _DOCKER_FORWARD_ENV which
    # forwards a host var by name — so it works even with no such var on the host.
    monkeypatch.delenv('TRAILS_DISABLED', raising=False)
    argv = build_argv(extra_env={'TRAILS_DISABLED': '1'})
    assert 'TRAILS_DISABLED=1' in argv
    assert argv[argv.index('TRAILS_DISABLED=1') - 1] == '-e'

  def test_no_extra_env_by_default(self, build_argv):
    assert not any('TRAILS_DISABLED' in a for a in build_argv())


class TestPppTarball:
  def _entries(self, blob: bytes) -> dict:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      return {m.name: m for m in tar.getmembers()}

  def test_prefixes_ppp_and_round_trips_content(self):
    blob = cw._ppp_tarball({'notion.json': b'{"token": "t"}', 'credentials.json': b'{}'})
    members = self._entries(blob)
    assert set(members) == {'.ppp', '.ppp/notion.json', '.ppp/credentials.json'}

    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r') as tar:
      extracted = tar.extractfile('.ppp/notion.json')
      assert extracted is not None
      assert extracted.read() == b'{"token": "t"}'

  def test_modes_and_owner(self):
    members = self._entries(cw._ppp_tarball({'notion.json': b'x'}))
    assert members['.ppp'].isdir()
    assert members['.ppp'].mode == 0o700
    assert members['.ppp/notion.json'].mode == 0o600
    # owned by the host uid/gid — the same uid the entrypoint remaps cw to on Linux
    assert members['.ppp/notion.json'].uid == cw.os.getuid()
    assert members['.ppp/notion.json'].gid == cw.os.getgid()


class TestRunInContainerInjection:
  @pytest.fixture
  def harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw, '_project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(cw, '_image_tag', lambda: 'tag')
    monkeypatch.setattr(cw, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(cw.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(cw, '_docker_create_argv', lambda *a, **k: ['docker', 'create', 'ARGS'])
    monkeypatch.setattr(
      cw.credentials, 'build_scoped_store', lambda names, optional=(): {'credentials.json': b'{}'}
    )
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append({'argv': argv, 'input': k.get('input')})
      if argv[0] == 'git':
        return _FakeProc(returncode=0)
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:2] == ['docker', 'start']:
        return _FakeProc(returncode=7)
      return _FakeProc(returncode=0)  # cp, rm

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    return calls

  def test_create_cp_start_sequence(self, harness):
    import io
    import tarfile

    code = cw.run_in_container('ws', ['claude'])
    assert code == 7  # propagates `docker start` exit code
    docker_calls = [c for c in harness if c['argv'][0] == 'docker']
    assert docker_calls[0]['argv'][:2] == ['docker', 'create']
    # the scoped store is cp'd into the pre-start container as a tar on stdin
    cp = next(c for c in harness if c['argv'][:3] == ['docker', 'cp', '-'])
    assert cp['argv'][3] == 'cid123:/home/cw'
    assert isinstance(cp['input'], bytes)
    with tarfile.open(fileobj=io.BytesIO(cp['input']), mode='r') as tar:
      assert '.ppp/credentials.json' in tar.getnames()
    # last call is the run-equivalent `docker start -a -i <id>`
    assert harness[-1]['argv'] == ['docker', 'start', '-a', '-i', 'cid123']

  def test_cp_failure_removes_container_and_raises(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw, '_project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(cw, '_image_tag', lambda: 'tag')
    monkeypatch.setattr(cw, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(cw.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(cw, '_docker_create_argv', lambda *a, **k: ['docker', 'create'])
    monkeypatch.setattr(
      cw.credentials, 'build_scoped_store', lambda names, optional=(): {'credentials.json': b'{}'}
    )
    removed: list = []

    def fake_run(argv, *a, **k):
      if argv[0] == 'git':
        return _FakeProc(returncode=0)
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:3] == ['docker', 'cp', '-']:
        return _FakeProc(returncode=1, stderr=b'no such container')
      if argv[:3] == ['docker', 'rm', '-f']:
        removed.append(argv)
        return _FakeProc(returncode=0)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(cw.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError, match='docker cp'):
      cw.run_in_container('ws', ['claude'])
    assert removed == [['docker', 'rm', '-f', 'cid123']]
