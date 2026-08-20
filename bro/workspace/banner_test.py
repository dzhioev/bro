import pytest

import bro.workspace.banner as workspace_banner
import bro.workspace.paths as workspace_paths
from bro import summon
from bro.monitor import trail_pointer
from bro.workspace.banner import SessionFacts


class TestSplitLaunchPrompt:
  def test_new_marker(self):
    head, prompt = workspace_banner._split_launch_prompt('dive-in --hold attended --new I want X')
    assert head == 'dive-in --hold attended --new '
    assert prompt == 'I want X'

  def test_no_marker_returns_command_unchanged(self):
    head, prompt = workspace_banner._split_launch_prompt('dive-in -t abc123')
    assert head == 'dive-in -t abc123'
    assert prompt is None

  def test_marker_without_trailing_content_is_not_a_match(self):
    # `dive-in --new ` with no seed should not produce an empty prompt
    head, prompt = workspace_banner._split_launch_prompt('dive-in --hold attended --new ')
    assert prompt is None
    assert head == 'dive-in --hold attended --new '


class TestSessionFacts:
  @pytest.fixture(autouse=True)
  def isolate_env(self, monkeypatch, tmp_path):
    # session-related env vars are picked up directly — wipe to a known state,
    # stub the /.dockerenv probe so host runs don't accidentally read True, and
    # point the session state dir at an empty one so the session-local reads
    # (trail pointer, recording health) see a session that publishes nothing
    for v in (
      'RIDE_WORKSPACE',
      'RIDE_BRO',
      'RIDE_COMMAND',
      'BRO_SHELL_COMMAND',
      'RIDE_HOST_WORKSPACE',
      'RIDE_REPO',
      summon.MAY_SUMMON_ENV,
    ):
      monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setattr(workspace_paths, 'in_container', lambda: False)

  def test_container_session(self, monkeypatch):
    monkeypatch.setattr(workspace_paths, 'in_container', lambda: True)
    monkeypatch.setenv('RIDE_WORKSPACE', 'my-task')
    monkeypatch.setenv('RIDE_BRO', 'dev')
    monkeypatch.setenv('RIDE_HOST_WORKSPACE', '/var/ride/0123456789abcdef/workspaces/my-task/tree')
    monkeypatch.setenv('BRO_SHELL_COMMAND', 'dive-in -t abc')
    monkeypatch.setenv('RIDE_COMMAND', 'ride along --hold attended my-task')
    facts = SessionFacts.collect()
    assert facts.in_container is True
    assert facts.name == 'my-task'
    assert facts.bro == 'dev'
    assert facts.host_workspace == '/var/ride/0123456789abcdef/workspaces/my-task/tree'
    assert facts.container_workspace == '/workspace'
    assert facts.exec_command == 'ride exec my-task'
    assert facts.shell_command == 'dive-in -t abc'
    assert facts.ride_command == 'ride along --hold attended my-task'
    assert facts.prompt is None

  def test_unmanaged_container_has_no_workspace(self, monkeypatch):
    monkeypatch.setattr(workspace_paths, 'in_container', lambda: True)
    facts = SessionFacts.collect()
    assert facts.in_container is True
    assert facts.name is None
    assert facts.container_workspace is None
    assert facts.exec_command is None
    assert 'workspace_container_path' not in facts.render_llm()
    assert '(unmanaged container)' in facts.render_visual()

  def test_extracts_prompt_from_dive_in_new(self, monkeypatch):
    monkeypatch.setenv('BRO_SHELL_COMMAND', 'dive-in --hold attended --new I want X')
    facts = SessionFacts.collect()
    assert facts.shell_command == 'dive-in --hold attended --new '
    assert facts.prompt == 'I want X'

  def test_host_worktree_reads_paths_from_the_session_environment(self, monkeypatch, tmp_path):
    worktree = tmp_path / 'tree'
    monkeypatch.setenv('RIDE_WORKSPACE', 'feature')
    monkeypatch.setenv('RIDE_HOST_WORKSPACE', str(worktree))
    monkeypatch.setenv('RIDE_REPO', str(tmp_path / 'project'))
    monkeypatch.setenv('RIDE_COMMAND', 'ride along feature')
    facts = SessionFacts.collect()
    assert facts.in_container is False
    assert facts.name == 'feature'
    assert facts.repo == str(tmp_path / 'project')
    assert facts.bro is None
    assert facts.host_workspace == str(worktree)
    assert facts.container_workspace is None
    assert facts.exec_command is None
    assert facts.shell_command == 'ride along feature'
    assert facts.ride_command == 'ride along feature'

  def test_a_missing_host_path_is_not_derived_from_cwd(self, monkeypatch):
    monkeypatch.setenv('RIDE_WORKSPACE', 'feature')
    assert SessionFacts.collect().host_workspace is None

  def test_shell_command_falls_back_to_ride_command(self, monkeypatch):
    monkeypatch.setenv('RIDE_COMMAND', 'ride along x')
    facts = SessionFacts.collect()
    assert facts.shell_command == 'ride along x'
    assert facts.ride_command == 'ride along x'

  def test_no_session_context(self):
    facts = SessionFacts.collect()
    assert facts.in_container is False
    assert facts.name is None
    assert facts.bro is None
    assert facts.host_workspace is None
    assert facts.exec_command is None
    assert facts.shell_command is None
    assert facts.ride_command is None
    assert facts.prompt is None
    assert facts.may_summon is None
    assert facts.trail_id is None

  def test_may_summon_reads_the_launch_published_list(self, monkeypatch):
    monkeypatch.setenv(summon.MAY_SUMMON_ENV, 'dev,reviewer')
    assert SessionFacts.collect().may_summon == ('dev', 'reviewer')

  def test_may_summon_distinguishes_an_empty_list_from_an_unset_one(self, monkeypatch):
    monkeypatch.setenv(summon.MAY_SUMMON_ENV, '')
    assert SessionFacts.collect().may_summon == ()

  def test_trail_id_reads_the_session_pointer(self):
    trail_pointer.publish('01trail')
    assert SessionFacts.collect().trail_id == '01trail'

  def test_trail_id_override_wins_over_the_pointer(self):
    # an in-process run records its own trail; the pointer names the claude
    # session it was started from
    trail_pointer.publish('01session')
    assert SessionFacts.collect(trail_id_override='01run').trail_id == '01run'

  def test_bro_override_takes_precedence_over_env(self, monkeypatch):
    # an in-process caller passes its bro explicitly — its environment carries
    # the launcher's RIDE_BRO (or none); the override wins either way
    monkeypatch.delenv('RIDE_BRO', raising=False)
    assert SessionFacts.collect(bro_override='researcher').bro == 'researcher'
    monkeypatch.setenv('RIDE_BRO', 'dev')
    assert SessionFacts.collect(bro_override='researcher').bro == 'researcher'
    assert SessionFacts.collect().bro == 'dev'


def _facts(**overrides) -> SessionFacts:
  base = {
    'in_container': True,
    'name': 'task',
    'bro': None,
    'host_workspace': '/h/ws',
    'container_workspace': '/workspace',
    'exec_command': 'ride exec task',
    'ride_command': None,
    'shell_command': None,
    'prompt': None,
    'recording_problem': None,
    'may_summon': None,
    'trail_id': None,
  }
  base.update(overrides)
  return SessionFacts(**base)


class TestRenderBanner:
  def test_llm_emits_plain_key_value(self):
    out = _facts(
      bro='dev',
      ride_command='ride along --bro bro task',
      shell_command='dive-in -t x',
    ).render_llm()
    assert '\033[' not in out  # no ANSI
    assert '██' not in out  # no logo
    assert 'kind: container' in out
    assert 'name: task' in out
    assert 'bro: dev' in out
    assert 'workspace_host_path: /h/ws' in out
    assert 'workspace_container_path: /workspace' in out
    assert 'docker_shell_command: ride exec task' in out
    assert 'ride_command: ride along --bro bro task' in out
    assert 'launch_command:' not in out
    assert 'dive-in -t x' not in out

  def test_llm_excludes_launch_prompt(self):
    out = _facts(shell_command='dive-in --new ', prompt='I want X').render_llm()
    assert 'I want X' not in out
    assert 'prompt:' not in out
    assert 'launch_command:' not in out

  def test_llm_emits_ride_command_for_direct_session(self):
    out = _facts(ride_command='ride along feature', shell_command='ride along feature').render_llm()
    assert 'ride_command: ride along feature' in out
    assert 'launch_command:' not in out

  def test_llm_omits_none_fields(self):
    out = _facts(
      in_container=False,
      name=None,
      host_workspace=None,
      container_workspace=None,
      exec_command=None,
    ).render_llm()
    assert out == 'kind: worktree\nrepo: none (detached)\ntrail_id: none (not published)'

  def test_llm_lists_the_summon_targets(self):
    assert 'may_summon: dev, reviewer' in _facts(may_summon=('dev', 'reviewer')).render_llm()

  def test_llm_spells_out_an_empty_allow_list(self):
    # distinguishable from the omitted line of a launch that published no list
    assert 'may_summon: none' in _facts(may_summon=()).render_llm()

  def test_llm_omits_may_summon_when_no_list_was_published(self):
    assert 'may_summon' not in _facts(may_summon=None).render_llm()

  def test_llm_emits_the_trail_id(self):
    assert 'trail_id: 01trail' in _facts(trail_id='01trail').render_llm()

  def test_visual_shows_the_summon_targets_and_trail(self):
    out = _facts(may_summon=('dev',), trail_id='01trail').render_visual()
    assert 'may summon:   \033[2mdev\033[0m' in out
    assert 'trail:        \033[2m01trail\033[0m' in out

  def test_visual_shows_an_empty_allow_list(self):
    assert '(none)' in _facts(may_summon=()).render_visual()

  def test_visual_omits_the_unpublished_facts(self):
    out = _facts().render_visual()
    assert 'may summon:' not in out
    assert 'trail:' not in out

  def test_visual_shows_logo_with_bro_signature(self):
    out = _facts(bro='dev').render_visual()
    # logo present (top five lines unchanged); bottom line gets a `// <bro>`
    # signature — dim slashes, bright-white-bold bro name
    for line in workspace_banner._BRO_LOGO.split('\n')[:-1]:
      assert line in out
    assert '\033[2m//\033[0m \033[1;97mdev\033[0m' in out

  def test_visual_session_line_shows_the_workspace_name(self):
    out = _facts(name='task').render_visual()
    assert 'ride session: \033[1mtask\033[0m' in out

  def test_visual_container_shows_workspace_and_host_path_on_separate_lines(self):
    out = _facts(host_workspace='/var/ride/0123456789abcdef/workspaces/task/tree').render_visual()
    assert 'workspace:    /workspace' in out
    assert 'host path:    \033[2m/var/ride/0123456789abcdef/workspaces/task/tree\033[0m' in out

  def test_visual_uses_docker_shell_label(self):
    out = _facts().render_visual()
    assert 'docker shell:' in out
    assert 'host shell:' not in out

  def test_visual_session_line_on_a_worktree(self):
    out = _facts(
      in_container=False,
      name='feature',
      host_workspace='/var/ride/fedcba9876543210/workspaces/feature/tree',
      container_workspace=None,
      exec_command=None,
    ).render_visual()
    # in worktree mode there's no `docker shell:` row, so the widest label is
    # `ride session:` (11 chars) — value follows with one space, no extra padding
    assert 'ride session: \033[1mfeature\033[0m' in out

  def test_visual_skips_logo_for_non_bro(self):
    out = _facts().render_visual()
    assert '██' not in out

  def test_visual_paints_host_path_red_for_worktree(self):
    out = _facts(
      in_container=False,
      name='feature',
      host_workspace='/var/ride/fedcba9876543210/workspaces/feature/tree',
      container_workspace=None,
      exec_command=None,
    ).render_visual()
    assert '\033[31m/var/ride/fedcba9876543210/workspaces/feature/tree\033[0m' in out

  def test_visual_does_not_paint_container_path_red(self):
    out = _facts(host_workspace='/var/ride/0123456789abcdef/workspaces/task/tree').render_visual()
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

  def test_visual_shows_ride_command_when_distinct(self):
    out = _facts(
      ride_command='ride along --bro bro task', shell_command='dive-in -t x'
    ).render_visual()
    assert 'ride command:' in out
    assert 'ride along --bro bro task' in out

  def test_visual_suppresses_ride_command_when_equal(self):
    out = _facts(
      ride_command='ride along feature', shell_command='ride along feature'
    ).render_visual()
    assert 'ride command:' not in out

  def test_visual_replaces_prompt_with_placeholder_and_separate_line(self):
    out = _facts(shell_command='dive-in --new ', prompt='I want a banner').render_visual()
    # bright-white bold for both the @prompt@ placeholder and the prompt: label
    # (the label style wraps the padded label, so trailing spaces sit inside)
    assert '\033[1;97m@prompt@\033[0m' in out
    assert '\033[1;97mprompt:' in out and '\033[0m' in out
    # the actual prompt text appears once, on its own line
    assert 'I want a banner' in out
    assert out.count('I want a banner') == 1

  def test_llm_emits_the_recording_problem_as_first_line(self):
    out = _facts(recording_problem='FAILING — see session-recorder.log').render_llm()
    # first line so it survives Claude's collapsed tool-output preview
    assert out.splitlines()[0] == 'session_recording: FAILING — see session-recorder.log'
    assert 'kind: container' in out

  def test_llm_omits_the_recording_problem_when_healthy(self):
    out = _facts().render_llm()
    assert 'session_recording' not in out

  def test_visual_paints_the_recording_problem_red_above_logo(self):
    out = _facts(bro='dev', recording_problem='STOPPED — the recorder is gone').render_visual()
    first = out.splitlines()[0]
    assert first.startswith('\033[31m\033[1m⚠ ')
    assert 'session recording STOPPED — the recorder is gone' in first
    # warning sits above the bro logo
    assert out.index('⚠') < out.index('██')
