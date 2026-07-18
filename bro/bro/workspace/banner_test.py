import pytest

import workspace.banner
import workspace.paths
from workspace.banner import SessionFacts


class TestSplitLaunchPrompt:
  def test_new_marker(self):
    head, prompt = workspace.banner._split_launch_prompt('dive-in --mode attended --new I want X')
    assert head == 'dive-in --mode attended --new '
    assert prompt == 'I want X'

  def test_dashdash_marker(self):
    head, prompt = workspace.banner._split_launch_prompt('cw ss name -- run the thing')
    assert head == 'cw ss name -- '
    assert prompt == 'run the thing'

  def test_p_marker(self):
    head, prompt = workspace.banner._split_launch_prompt('cw ss -p hello world')
    assert head == 'cw ss -p '
    assert prompt == 'hello world'

  def test_long_prompt_marker(self):
    head, prompt = workspace.banner._split_launch_prompt('cw ss --prompt do the thing')
    assert head == 'cw ss --prompt '
    assert prompt == 'do the thing'

  def test_no_marker_returns_command_unchanged(self):
    head, prompt = workspace.banner._split_launch_prompt('dive-in -t abc123')
    assert head == 'dive-in -t abc123'
    assert prompt is None

  def test_marker_without_trailing_content_is_not_a_match(self):
    # `dive-in --new ` with no seed should not produce an empty prompt
    head, prompt = workspace.banner._split_launch_prompt('dive-in --mode attended --new ')
    assert prompt is None
    assert head == 'dive-in --mode attended --new '


class TestSessionFacts:
  @pytest.fixture(autouse=True)
  def isolate_env(self, monkeypatch):
    # session-related env vars are picked up directly — wipe to a known state,
    # and stub the /.dockerenv probe so host runs don't accidentally read True
    for v in ('CW_NAME', 'CW_BRO', 'CW_COMMAND', 'PPP_SHELL_COMMAND', 'CW_HOST_WORKSPACE'):
      monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(workspace.paths, 'in_container', lambda: False)

  def test_container_session(self, monkeypatch):
    monkeypatch.setattr(workspace.paths, 'in_container', lambda: True)
    monkeypatch.setenv('CW_NAME', 'my-task')
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    monkeypatch.setenv('CW_HOST_WORKSPACE', '/host/var/cw/containers/my-task')
    monkeypatch.setenv('PPP_SHELL_COMMAND', 'dive-in -t abc')
    monkeypatch.setenv('CW_COMMAND', 'cw ss --mode attended my-task')
    facts = SessionFacts.collect()
    assert facts.in_container is True
    assert facts.name == 'my-task'
    assert facts.bro == 'ppp-dev'
    assert facts.host_workspace == '/host/var/cw/containers/my-task'
    assert facts.container_workspace == '/workspace'
    assert facts.exec_command == 'cw exec my-task'
    assert facts.shell_command == 'dive-in -t abc'
    assert facts.cw_command == 'cw ss --mode attended my-task'
    assert facts.prompt is None

  def test_extracts_prompt_from_dive_in_new(self, monkeypatch):
    monkeypatch.setenv('PPP_SHELL_COMMAND', 'dive-in --mode attended --new I want X')
    facts = SessionFacts.collect()
    assert facts.shell_command == 'dive-in --mode attended --new '
    assert facts.prompt == 'I want X'

  def test_host_worktree_with_derived_path(self, monkeypatch, tmp_path):
    project = tmp_path / 'project'
    worktree = project / 'var' / 'cw' / 'worktrees' / 'feature'
    worktree.mkdir(parents=True)
    monkeypatch.setattr(workspace.paths, 'project_root', lambda: project)
    monkeypatch.setenv('CW_NAME', 'feature')
    monkeypatch.setenv('CW_COMMAND', 'cw ss feature')
    facts = SessionFacts.collect()
    assert facts.in_container is False
    assert facts.name == 'feature'
    assert facts.bro is None
    assert facts.host_workspace == str(worktree)
    assert facts.container_workspace is None
    assert facts.exec_command is None
    # PPP_SHELL_COMMAND defaults to CW_COMMAND — they're equal in this case
    assert facts.shell_command == 'cw ss feature'
    assert facts.cw_command == 'cw ss feature'

  def test_shell_command_falls_back_to_cw_command(self, monkeypatch):
    monkeypatch.setenv('CW_COMMAND', 'cw ss x')
    facts = SessionFacts.collect()
    assert facts.shell_command == 'cw ss x'
    assert facts.cw_command == 'cw ss x'

  def test_no_session_context(self):
    facts = SessionFacts.collect()
    assert facts.in_container is False
    assert facts.name is None
    assert facts.bro is None
    assert facts.host_workspace is None
    assert facts.exec_command is None
    assert facts.shell_command is None
    assert facts.cw_command is None
    assert facts.prompt is None

  def test_bro_override_takes_precedence_over_env(self, monkeypatch):
    # an in-process caller passes its bro explicitly — its environment carries
    # the launcher's CW_BRO (or none); the override wins either way
    monkeypatch.delenv('CW_BRO', raising=False)
    assert SessionFacts.collect(bro_override='librorian').bro == 'librorian'
    monkeypatch.setenv('CW_BRO', 'pm')
    assert SessionFacts.collect(bro_override='librorian').bro == 'librorian'
    assert SessionFacts.collect().bro == 'pm'


def _facts(**overrides) -> SessionFacts:
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
  return SessionFacts(**base)


class TestRenderBanner:
  def test_llm_emits_plain_key_value(self):
    out = _facts(
      bro='ppp-dev',
      cw_command='cw ss --persona pm task',
      shell_command='dive-in -t x',
    ).render_llm()
    assert '\033[' not in out  # no ANSI
    assert '██' not in out  # no logo
    assert 'kind: container' in out
    assert 'name: task' in out
    assert 'bro: ppp-dev' in out
    assert 'workspace_host_path: /h/ws' in out
    assert 'workspace_container_path: /workspace' in out
    assert 'docker_shell_command: cw exec task' in out
    assert 'cw_command: cw ss --persona pm task' in out
    assert 'launch_command:' not in out
    assert 'dive-in -t x' not in out

  def test_llm_excludes_launch_prompt(self):
    out = _facts(shell_command='dive-in --new ', prompt='I want X').render_llm()
    assert 'I want X' not in out
    assert 'prompt:' not in out
    assert 'launch_command:' not in out

  def test_llm_emits_cw_command_for_direct_session(self):
    out = _facts(cw_command='cw ss feature', shell_command='cw ss feature').render_llm()
    assert 'cw_command: cw ss feature' in out
    assert 'launch_command:' not in out

  def test_llm_omits_none_fields(self):
    out = _facts(
      in_container=False,
      name=None,
      host_workspace=None,
      container_workspace=None,
      exec_command=None,
    ).render_llm()
    assert out == 'kind: host worktree'

  def test_visual_shows_logo_with_bro_signature(self):
    out = _facts(bro='pm').render_visual()
    # logo present (top five lines unchanged); bottom line gets a `// <bro>`
    # signature — dim slashes, bright-white-bold bro name
    for line in workspace.banner._BRO_LOGO.split('\n')[:-1]:
      assert line in out
    assert '\033[2m//\033[0m \033[1;97mpm\033[0m' in out
    # no parens-form kind on the session line — encoded by the c: prefix instead
    assert '(container)' not in out
    assert '(host worktree)' not in out

  def test_visual_session_line_uses_c_prefix_for_container(self):
    out = _facts(name='task').render_visual()
    assert 'cw session:   \033[1mc:task\033[0m' in out

  def test_visual_container_shows_workspace_and_host_path_on_separate_lines(self):
    out = _facts(host_workspace='/host/var/cw/containers/task').render_visual()
    assert 'workspace:    /workspace' in out
    assert 'host path:    \033[2m/host/var/cw/containers/task\033[0m' in out

  def test_visual_uses_docker_shell_label(self):
    out = _facts().render_visual()
    assert 'docker shell:' in out
    assert 'host shell:' not in out

  def test_visual_session_line_omits_prefix_for_worktree(self):
    out = _facts(
      in_container=False,
      name='feature',
      host_workspace='/project/var/cw/worktrees/feature',
      container_workspace=None,
      exec_command=None,
    ).render_visual()
    # in worktree mode there's no `docker shell:` row, so the widest label is
    # `cw session:` (11 chars) — value follows with one space, no extra padding
    assert 'cw session: \033[1mfeature\033[0m' in out
    assert 'c:feature' not in out

  def test_visual_skips_logo_for_non_bro(self):
    out = _facts().render_visual()
    assert '██' not in out

  def test_visual_paints_host_path_red_for_worktree(self):
    out = _facts(
      in_container=False,
      name='feature',
      host_workspace='/project/var/cw/worktrees/feature',
      container_workspace=None,
      exec_command=None,
    ).render_visual()
    assert '\033[31m/project/var/cw/worktrees/feature\033[0m' in out

  def test_visual_does_not_paint_container_path_red(self):
    out = _facts(host_workspace='/host/var/cw/containers/task').render_visual()
    assert '\033[31m' not in out

  def test_visual_handles_missing_host_path_in_worktree(self):
    out = _facts(
      in_container=False,
      name=None,
      host_workspace=None,
      container_workspace=None,
      exec_command=None,
    ).render_visual()
    assert '(unknown' in out

  def test_visual_shows_cw_command_when_distinct(self):
    out = _facts(cw_command='cw ss --persona pm task', shell_command='dive-in -t x').render_visual()
    assert 'cw command:' in out
    assert 'cw ss --persona pm task' in out

  def test_visual_suppresses_cw_command_when_equal(self):
    out = _facts(cw_command='cw ss feature', shell_command='cw ss feature').render_visual()
    assert 'cw command:' not in out

  def test_visual_replaces_prompt_with_placeholder_and_separate_line(self):
    out = _facts(shell_command='dive-in --new ', prompt='I want a banner').render_visual()
    # bright-white bold for both the @prompt@ placeholder and the prompt: label
    # (the label style wraps the padded label, so trailing spaces sit inside)
    assert '\033[1;97m@prompt@\033[0m' in out
    assert '\033[1;97mprompt:' in out and '\033[0m' in out
    # the actual prompt text appears once, on its own line
    assert 'I want a banner' in out
    assert out.count('I want a banner') == 1

  def test_llm_emits_sync_warning_as_first_line(self):
    out = _facts(
      sync_warning='session-log sync FAILING — run setup/bootstrap_session_log.sh'
    ).render_llm()
    # first line so it survives Claude's collapsed tool-output preview
    assert out.splitlines()[0] == 'session_log_sync: FAILING — run setup/bootstrap_session_log.sh'
    assert 'kind: container' in out

  def test_llm_omits_sync_warning_when_healthy(self):
    out = _facts().render_llm()
    assert 'session_log_sync' not in out

  def test_visual_paints_sync_warning_red_above_logo(self):
    out = _facts(
      bro='pm', sync_warning='session-log sync FAILING — run setup/bootstrap_session_log.sh'
    ).render_visual()
    first = out.splitlines()[0]
    assert first.startswith('\033[31m\033[1m⚠ ')
    assert 'session-log sync FAILING' in first
    # warning sits above the bro logo
    assert out.index('⚠') < out.index('██')
