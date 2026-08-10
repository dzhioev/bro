import json
from typing import Optional
from unittest.mock import patch

import pytest

import bro.cw.session as cw_session
import bro.launch.scope
import bro.launch.summon_control
import bro.workspace.project as workspace_project
from bro.cw.flags import DEFAULT_HOLD
from bro.launch.scope import ScopedSecrets


def _spec(
  *,
  name: str = 'w',
  host: bool = False,
  drop: bool = False,
  hold: str = DEFAULT_HOLD,
  fast: bool = False,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  effort: Optional[str] = None,
  resume: bool = False,
  into: Optional[str] = None,
  bro: Optional[str] = None,
  raw: bool = False,
  prompt: Optional[str] = None,
  claude_args: Optional[list[str]] = None,
) -> cw_session.SessionSpec:
  return cw_session.SessionSpec(
    name=name,
    host=host,
    drop=drop,
    hold=hold,
    fast=fast,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
    effort=effort,
    resume=resume,
    into=into,
    bro=bro,
    raw=raw,
    prompt=prompt,
    claude_args=claude_args if claude_args is not None else [],
  )


@pytest.fixture(autouse=True)
def configured_project(monkeypatch, tmp_path):
  monkeypatch.setattr(
    cw_session,
    'project_config',
    lambda: workspace_project.ProjectConfig(default_bro='bro-dev', image_repository='bro/bro-dev'),
  )
  # every launch path takes the workspace session lock and records a resume spec
  # under the project root; keep both off the real repo
  monkeypatch.setattr(cw_session, 'project_root', lambda: tmp_path)
  # the suite itself runs inside a container; without this every container launch
  # would degrade to host mode
  monkeypatch.delenv('CW_IN_CONTAINER', raising=False)


class _ContainerHarness:
  """patches for driving start_session through the container path without docker,
  bro imports, or git side effects."""

  def __init__(
    self, secrets: Optional[set[str]] = None, optional_secrets: Optional[set[str]] = None
  ):
    self.secrets = secrets if secrets is not None else {'github'}
    self.optional_secrets = optional_secrets if optional_secrets is not None else set()

  def __enter__(self):
    self._patches = [
      patch.dict('os.environ', {}, clear=False),
      patch('bro.cw.session.find_container_id', return_value=None),
      patch('bro.cw.session.run_in_container', return_value=0),
      patch(
        'bro.cw.session.scoped_secrets',
        return_value=ScopedSecrets(set(self.secrets), set(self.optional_secrets), True),
      ),
      patch('bro.cw.session.credentials.try_get', return_value='tok'),
      patch('bro.launch.scope.credentials.build_scoped_store', return_value={}),
      patch('bro.cw.session.container_claude_state', return_value=([], {})),
      patch('bro.cw.session.drop_workspace'),
      patch('bro.cw.session._replace_resume_hint'),
      # keep the bro-registry import out; threading is asserted per-test
      patch('bro.launch.summon_control.summon_allow_list', return_value=set()),
      patch('bro.cw.session._load_anthropic_key', return_value={'api_key': 'k'}),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('CW_BRO', None)
    self.env.pop('CW_IN_CONTAINER', None)
    self.run_in_container = entered[2]
    self.try_get = entered[4]
    self.build_scoped_store = entered[5]
    self.container_claude_state = entered[6]
    self.drop_workspace = entered[7]
    self.summon_allow_list = entered[9]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestGrantRevoke:
  def test_start_session_applies_grant_and_revoke(self):
    with _ContainerHarness(secrets={'notion', 'trails', 'github'}) as h:
      rc = cw_session.start_session(_spec(drop=True, grant=['gmail_creds'], revoke=['notion']))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'gmail_creds' in launch.secrets
    assert 'notion' not in launch.secrets

  def test_start_session_grant_replaces_a_credential_instance(self):
    with _ContainerHarness(secrets={'brog', 'github'}) as harness:
      rc = cw_session.start_session(_spec(drop=True, grant=['brog+github']))
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.secrets == {'brog+github', 'github'}

  def test_start_session_can_revoke_an_optional_secret(self):
    with _ContainerHarness(optional_secrets={'openai'}) as harness:
      rc = cw_session.start_session(_spec(drop=True, revoke=['openai']))
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.optional_secrets == set()

  def test_missing_secret_fails_cleanly_before_container_launch(self, caplog):
    with _ContainerHarness() as harness:
      harness.build_scoped_store.side_effect = cw_session.credentials.SecretNotFound('github')
      rc = cw_session.start_session(_spec(drop=True))
    assert rc == 1
    assert harness.run_in_container.call_count == 0
    assert 'github' in caplog.text

  def test_missing_setup_token_has_actionable_container_error(self, caplog):
    with _ContainerHarness() as harness:
      harness.try_get.return_value = None
      rc = cw_session.start_session(_spec(drop=True))
    assert rc == 1
    assert harness.run_in_container.call_count == 0
    assert 'mint one with `claude setup-token`' in caplog.text

  def test_missing_setup_token_does_not_gate_a_raw_launch(self):
    with _ContainerHarness() as harness:
      harness.try_get.return_value = None
      rc = cw_session.start_session(_spec(drop=True, raw=True))
    assert rc == 0

  def test_start_session_grant_already_present_returns_1(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True, grant=['github']))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_start_session_injects_effort_into_the_container_command(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True, effort='xhigh'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command[command.index('--effort') + 1] == 'xhigh'


class TestSummonAllowList:
  def test_container_session_threads_the_allow_list(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.return_value = {'dev'}
      rc = cw_session.start_session(_spec(drop=True, grant=['@dev']))
    assert rc == 0
    assert h.summon_allow_list.call_args == (
      ('bro-dev',),
      {'grant': ['dev'], 'revoke': []},
    )
    assert h.run_in_container.call_args.kwargs['may_summon'] == {'dev'}

  def test_container_session_keys_identity_on_the_bro(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True, bro='dev'))
    assert rc == 0
    assert h.summon_allow_list.call_args[0] == ('dev',)

  def test_bad_summon_flag_fails_the_launch(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.side_effect = ValueError('unknown summon target(s): devoop')
      rc = cw_session.start_session(_spec(drop=True, grant=['@devoop']))
    assert rc == 1
    assert h.run_in_container.call_count == 0


class TestContainerCommand:
  def test_command_is_the_in_place_invocation(self):
    # the docker command is the same in-place runner host mode spawns; the
    # argv/MCP/script-delivery work happens inside the container, next to claude
    with _ContainerHarness() as h:
      rc = cw_session.start_session(
        _spec(drop=True, fast=True, bro='dev', effort='xhigh', prompt='go')
      )
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'cw', 'ss', '--in-place', '--fast', '--effort', 'xhigh', '--bro', 'dev', '--prompt=go', 'w',
    ]  # fmt: skip

  def test_bro_carried_in_command_and_stamped_into_the_container_env(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True, bro='dev'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == ['cw', 'ss', '--in-place', '--bro', 'dev', 'w']
    # CW_BRO themes the whole container (cw exec shells), set explicitly in the
    # container env — never forwarded from the launcher's environment
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['CW_BRO'] == 'dev'

  def test_raw_carried_in_the_container_command(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True, bro='dev', raw=True))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == ['cw', 'ss', '--in-place', '--raw', '--bro', 'dev', 'w']

  def test_cw_session_stamps_the_default_bro_as_cw_bro(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['CW_BRO'] == 'bro-dev'

  def test_default_base_is_left_to_the_entrypoint_head_fallback(self):
    # no CW_BASE_REF by default: the clone bases on HEAD — the host checkout as
    # cloned — with no network touched on the way
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'CW_BASE_REF' not in launch.env

  def test_into_threads_the_resolved_base_into_the_container_env(self):
    with _ContainerHarness() as h:
      with patch('bro.cw.session.resolve_ref', return_value='intosha') as resolve:
        rc = cw_session.start_session(_spec(drop=True, into='feature'))
    assert rc == 0
    assert resolve.call_args[0][1] == 'feature'
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['CW_BASE_REF'] == 'intosha'

  def test_unresolvable_into_fails_launch(self):
    with _ContainerHarness() as h:
      with patch('bro.cw.session.resolve_ref', return_value=None):
        rc = cw_session.start_session(_spec(drop=True, into='nope'))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_guard_fails_fast_without_a_session(self, tmp_path):
    with _ContainerHarness() as h:
      with patch('bro.cw.session.workspace_projects_dir') as projects:
        projects.return_value = tmp_path / 'projects'
        rc = cw_session.start_session(_spec(resume=True))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_carried_as_bare_flag_the_runner_resolves(self, tmp_path):
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'abc.jsonl').write_text('{}')
    with _ContainerHarness() as h:
      with patch('bro.cw.session.workspace_projects_dir') as projects:
        projects.return_value = projects_dir
        rc = cw_session.start_session(_spec(resume=True))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == ['cw', 'ss', '--in-place', '--resume', 'w']


class TestContainerDrop:
  def test_drop_removes_the_workspace_on_clean_exit(self):
    with _ContainerHarness() as h:
      rc = cw_session.start_session(_spec(drop=True))
    assert rc == 0
    assert h.drop_workspace.call_count == 1

  def test_drop_keeps_the_workspace_when_the_session_failed(self):
    with _ContainerHarness() as h:
      h.run_in_container.return_value = 3
      rc = cw_session.start_session(_spec(drop=True))
    assert rc == 3
    assert h.drop_workspace.call_count == 0


class TestCommandArgv:
  def test_create_command_includes_drop_into_and_claude_args(self):
    parts = _spec(
      hold='attended',
      fast=True,
      drop=True,
      effort='xhigh',
      bro='dev',
      grant=['gmail_creds', '@bro'],
      revoke=['notion'],
      into='feature',
      claude_args=['--foo'],
    ).to_command_argv()
    assert parts == [
      'cw', 'ss', '--fast', '--drop', '--hold', 'attended',
      '--effort', 'xhigh', '--bro', 'dev', '--grant', 'gmail_creds',
      '--grant', '@bro', '--revoke', 'notion', '--into', 'feature', 'w', '--foo',
    ]  # fmt: skip

  def test_host_session_carries_the_host_flag(self):
    parts = _spec(host=True, hold='detached').to_command_argv()
    assert parts == ['cw', 'ss', '--host', '--hold', 'detached', 'w']

  def test_default_hold_is_elided(self):
    # the parser's default hold stays implicit in the reconstructed command
    assert _spec().to_command_argv() == ['cw', 'ss', 'w']

  def test_a_resume_is_its_own_command(self):
    # the recorded spec carries the flags, so the ref is the whole command
    assert _spec(hold='attended', bro='dev').resume_variant().to_command_argv() == [
      'cw',
      'resume',
      'c:w',
    ]

  def test_a_host_resume_names_the_bare_ref(self):
    assert _spec(host=True).resume_variant().to_command_argv() == ['cw', 'resume', 'w']


class TestResumeSpecRecord:
  def test_recorded_spec_clears_create_only_inputs_and_round_trips(self, tmp_path):
    spec = _spec(
      hold='attended',
      drop=True,
      effort='xhigh',
      bro='dev',
      grant=['gmail_creds'],
      into='feature',
      prompt='do it',
      claude_args=['--foo'],
    )
    cw_session.record_resume_spec(tmp_path, spec)
    loaded = cw_session.load_resume_spec(tmp_path, 'c:w')
    assert loaded == spec.resume_variant()
    assert loaded is not None and loaded.resume and not loaded.drop
    assert loaded.into is None and loaded.prompt is None and loaded.claude_args == []
    # the forwarded flags survive, so the resumed session runs as it was launched
    assert (loaded.hold, loaded.effort, loaded.bro, loaded.grant) == (
      'attended',
      'xhigh',
      'dev',
      ['gmail_creds'],
    )

  def test_recording_a_resume_is_a_fixpoint(self, tmp_path):
    cw_session.record_resume_spec(tmp_path, _spec(bro='dev'))
    first = cw_session.load_resume_spec(tmp_path, 'c:w')
    assert first is not None
    cw_session.record_resume_spec(tmp_path, first)
    assert cw_session.load_resume_spec(tmp_path, 'c:w') == first

  def test_host_and_container_records_are_separate(self, tmp_path):
    cw_session.record_resume_spec(tmp_path, _spec(host=True, bro='host-bro'))
    cw_session.record_resume_spec(tmp_path, _spec(bro='container-bro'))
    host = cw_session.load_resume_spec(tmp_path, 'w')
    container = cw_session.load_resume_spec(tmp_path, 'c:w')
    assert host is not None and host.bro == 'host-bro'
    assert container is not None and container.bro == 'container-bro'

  def test_missing_record_reads_as_none(self, tmp_path):
    assert cw_session.load_resume_spec(tmp_path, 'c:w') is None

  def test_record_from_an_incompatible_cw_reads_as_none(self, tmp_path, caplog):
    file = tmp_path / 'var' / 'cw' / 'resume' / 'c:w.json'
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps({'name': 'w', 'gone': True}))
    assert cw_session.load_resume_spec(tmp_path, 'c:w') is None
    assert 'unreadable resume spec' in caplog.text

  def test_start_session_records_before_launching(self, tmp_path):
    with _ContainerHarness():
      recorded: list = []
      with patch(
        'bro.cw.session._container_session',
        side_effect=lambda *_a: recorded.append(cw_session.load_resume_spec(tmp_path, 'c:w')) or 0,
      ):
        assert cw_session.start_session(_spec(drop=True, bro='dev')) == 0
    assert recorded[0] == _spec(drop=True, bro='dev').resume_variant()


class TestResumeSession:
  def test_relaunches_the_recorded_spec(self, tmp_path):
    (tmp_path / 'var' / 'cw' / 'containers' / 'w').mkdir(parents=True)
    cw_session.record_resume_spec(tmp_path, _spec(bro='dev', hold='attended'))
    with patch('bro.cw.session.start_session', return_value=0) as start:
      assert cw_session.resume_session('c:w') == 0
    assert start.call_args[0][0] == _spec(bro='dev', hold='attended').resume_variant()

  def test_unknown_workspace_errors(self, caplog):
    with patch('bro.cw.session.start_session') as start:
      assert cw_session.resume_session('c:w') == 1
    assert start.call_count == 0
    assert 'container workspace not found: c:w' in caplog.text

  def test_workspace_without_a_record_errors(self, tmp_path, caplog):
    (tmp_path / 'var' / 'cw' / 'containers' / 'w').mkdir(parents=True)
    with patch('bro.cw.session.start_session') as start:
      assert cw_session.resume_session('c:w') == 1
    assert start.call_count == 0
    assert 'no session recorded for c:w' in caplog.text


class TestReplaceResumeHint:
  def test_prints_the_resume_command_over_claudes_hint(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cw_session, '_latest_jsonl', lambda directory: 'session.jsonl')
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    cw_session._replace_resume_hint(cw_session.ContainerWorkspace('w', tmp_path))
    assert 'cw resume c:w' in capsys.readouterr().out

  def test_silent_without_a_session_jsonl(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cw_session, '_latest_jsonl', lambda directory: None)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    cw_session._replace_resume_hint(cw_session.HostWorktree('w', tmp_path))
    assert capsys.readouterr().out == ''


class TestInPlaceArgv:
  def test_drops_machinery_flags_and_carries_the_rest(self):
    parts = _spec(
      host=True,
      hold='attended',
      fast=True,
      drop=True,
      effort='xhigh',
      bro='dev',
      grant=['gmail_creds'],
      revoke=['notion'],
      into='feature',
      prompt='do it',
      claude_args=['--foo'],
    ).to_in_place_argv()
    assert parts == [
      'ss', '--in-place', '--fast', '--hold', 'attended',
      '--effort', 'xhigh', '--bro', 'dev', '--prompt=do it', 'w', '--foo',
    ]  # fmt: skip

  def test_resume_and_raw_carried(self):
    parts = _spec(resume=True, bro='dev', raw=True).to_in_place_argv()
    assert parts == ['ss', '--in-place', '--resume', '--raw', '--bro', 'dev', 'w']


class TestSessionBro:
  def test_bro_names_the_identity(self):
    assert _spec(bro='dev').session_bro == 'dev'

  def test_session_uses_the_project_default(self):
    assert _spec().session_bro == 'bro-dev'

  def test_raw_selects_the_raw_surface(self):
    assert _spec(raw=True).surface == bro.launch.scope.Surface.RAW_SESSION
    assert _spec().surface == bro.launch.scope.Surface.CW_SESSION


class TestConcurrentSessionGuard:
  """the one-session-per-workspace lock, taken by start_session before either mode
  prepares anything."""

  def test_second_launch_is_refused_while_the_lock_is_held(self, tmp_path, caplog):
    workspace = cw_session.ContainerWorkspace('w', tmp_path)
    with workspace.hold_session_lock():
      with patch('bro.cw.session._container_session') as launch:
        assert cw_session.start_session(_spec()) == 1
    assert launch.call_count == 0
    assert 'session already active on workspace' in caplog.text

  def test_the_lock_releases_with_the_session(self, tmp_path):
    with patch('bro.cw.session._container_session', return_value=0):
      assert cw_session.start_session(_spec()) == 0
    assert not cw_session.ContainerWorkspace('w', tmp_path).is_active(set())

  def test_a_host_worktree_and_its_same_name_container_lock_apart(self, tmp_path):
    with cw_session.ContainerWorkspace('w', tmp_path).hold_session_lock():
      with patch('bro.cw.session._host_session', return_value=0) as launch:
        assert cw_session.start_session(_spec(host=True)) == 0
    assert launch.call_count == 1

  def test_container_refuses_an_orphaned_running_container(self, monkeypatch, tmp_path, caplog):
    # a launcher killed outright releases the lock but leaves its container bound
    monkeypatch.setattr(cw_session, 'find_container_id', lambda path: 'abc123')

    def boom(*_a, **_k):
      raise AssertionError('must not launch a second container session')

    monkeypatch.setattr(cw_session, 'run_in_container', boom)
    assert cw_session.start_session(_spec()) == 1
    assert 'session already active in the container' in caplog.text


class TestHostSession:
  def _fake_workspace(self, tmp_path, *, has_session: bool):
    worktree = tmp_path / 'wt'
    projects = tmp_path / 'projects'
    projects.mkdir()
    if has_session:
      (projects / 'abc.jsonl').write_text('{}')

    class _FakeHost(cw_session.HostWorktree):
      @property
      def path(self):
        return worktree

      def claude_projects_dir(self):
        return projects

      def remove(self):
        pass

    return _FakeHost('w', tmp_path), worktree

  def _prepare_launch(self, monkeypatch, tmp_path):
    workspace, worktree = self._fake_workspace(tmp_path, has_session=False)
    cw_bin = worktree / '.venv' / 'bin' / 'cw'
    cw_bin.parent.mkdir(parents=True)
    cw_bin.write_text('')
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(cw_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw_session, 'workspace_projects_dir', lambda ws: ws.claude_projects_dir())
    monkeypatch.setattr(cw_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw_session, 'provision_host_worktree', lambda *_a: True)
    # keep the launch tests off the real credential store; the auth-transform
    # test overrides this with its own fake
    monkeypatch.setattr(cw_session, '_apply_claude_auth', lambda env, **_k: None)
    monkeypatch.setattr(cw_session.credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      cw_session, '_provision_host_claude_dir', lambda name, wt, project: tmp_path / 'claude-config'
    )
    monkeypatch.setattr(
      cw_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets({'github'}, set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.setattr(
      cw_session,
      'materialize_scoped_store',
      lambda store, directory: directory / 'credentials.json',
    )
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    return workspace, cw_bin, worktree

  def test_broker_supervises_the_worktrees_own_in_place_runner(self, monkeypatch, tmp_path):
    workspace, cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: True)
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: {'dev'})
    roots: list = []

    def fake_root(name, command, worktree_arg, project, env, may_summon, credential_scope):
      roots.append(
        {
          'name': name,
          'command': command,
          'worktree': worktree_arg,
          'project': project,
          'env': env,
          'may_summon': may_summon,
          'credential_scope': credential_scope,
        }
      )
      return 5

    monkeypatch.setattr(cw_session, '_run_host_root_via_broker', fake_root)
    spec = _spec(host=True, hold='attended', effort='xhigh', prompt='go', claude_args=['--foo'])
    assert cw_session._host_session(spec, workspace, None) == 5
    assert roots[0]['name'] == 'w'
    assert roots[0]['command'] == [
      str(cw_bin), 'ss', '--in-place', '--hold', 'attended', '--effort', 'xhigh', '--prompt=go', 'w', '--foo',
    ]  # fmt: skip
    assert roots[0]['worktree'] == worktree
    assert roots[0]['project'] == tmp_path
    assert roots[0]['env']['VIRTUAL_ENV'] == str(worktree / '.venv')
    # the host root gets the session's summon allow-list like container mode
    assert roots[0]['may_summon'] == {'dev'}

  def test_bad_summon_flag_fails_before_the_worktree_is_ensured(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)

    def bad_allow_list(*_a, **_k):
      raise ValueError('unknown summon target(s): devoop')

    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', bad_allow_list)

    def boom(*_a, **_k):
      raise AssertionError('must not ensure a worktree when the summon grant is bad')

    monkeypatch.setattr(cw_session, 'ensure_host_worktree', boom)
    spec = _spec(host=True, grant=['@devoop'])
    assert cw_session._host_session(spec, workspace, None) == 1

  def test_direct_spawn_when_broker_disabled(self, monkeypatch, tmp_path):
    workspace, cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw_session.subprocess, 'run', fake_run)
    spec = _spec(host=True, hold='attended', effort='xhigh', prompt='go', claude_args=['--foo'])
    assert cw_session._host_session(spec, workspace, None) == 0
    argv, kwargs = runs[0]
    assert argv == [
      str(cw_bin), 'ss', '--in-place', '--hold', 'attended', '--effort', 'xhigh', '--prompt=go', 'w', '--foo',
    ]  # fmt: skip
    assert kwargs['cwd'] == str(worktree)
    assert kwargs['env']['VIRTUAL_ENV'] == str(worktree / '.venv')

  def test_resume_hint_is_the_last_step_on_success(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw_session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    events: list = []
    monkeypatch.setattr(cw_session, '_replace_resume_hint', lambda workspace: events.append('hint'))
    assert cw_session._host_session(_spec(host=True), workspace, None) == 0
    assert events == ['hint']

  def test_resume_hint_skipped_when_the_session_failed(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw_session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=3)
    )
    events: list = []
    monkeypatch.setattr(cw_session, '_replace_resume_hint', lambda workspace: events.append('hint'))
    assert cw_session._host_session(_spec(host=True), workspace, None) == 3
    assert events == []

  def test_drop_removes_the_worktree_and_skips_the_hint(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw_session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    events: list = []
    monkeypatch.setattr(cw_session, '_replace_resume_hint', lambda workspace: events.append('hint'))
    monkeypatch.setattr(cw_session, 'drop_workspace', lambda ws: events.append('remove'))
    assert cw_session._host_session(_spec(host=True, drop=True), workspace, None) == 0
    assert events == ['remove']

  def test_drop_keeps_the_worktree_when_the_session_failed(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw_session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=3)
    )
    events: list = []
    monkeypatch.setattr(cw_session, '_replace_resume_hint', lambda workspace: events.append('hint'))
    monkeypatch.setattr(cw_session, 'drop_workspace', lambda ws: events.append('remove'))
    assert cw_session._host_session(_spec(host=True, drop=True), workspace, None) == 3
    assert events == []
    # the failed end is recorded, so `cw clean` refuses the kept worktree
    assert (tmp_path / 'var' / 'cw' / 'exit' / 'w').read_text() == '3'

  def test_runner_env_gets_the_claude_auth_transform(self, monkeypatch, tmp_path):
    # the outer applies _apply_claude_auth to the runner env it spawns, so a
    # worktree whose own runner predates the transform still inherits the token
    workspace, cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)

    def fake_apply(env, **_kwargs):
      env['CLAUDE_CODE_OAUTH_TOKEN'] = 'applied'

    monkeypatch.setattr(cw_session, '_apply_claude_auth', fake_apply)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw_session.subprocess, 'run', fake_run)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 0
    assert runs[0][1]['env']['CLAUDE_CODE_OAUTH_TOKEN'] == 'applied'

  def test_runner_env_points_at_the_private_claude_config_dir(self, monkeypatch, tmp_path):
    # the outer provisions the per-session state dir and exports CLAUDE_CONFIG_DIR
    # itself, so a worktree whose own runner predates the config dir is covered too
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw_session.subprocess, 'run', fake_run)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 0
    assert runs[0][1]['env']['CLAUDE_CONFIG_DIR'] == str(tmp_path / 'claude-config')

  def test_missing_claude_code_fails_a_cw_session_launch_before_the_worktree(
    self, monkeypatch, tmp_path
  ):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session.credentials, 'try_get', lambda name: None)

    def boom(*_a, **_k):
      raise AssertionError('must not ensure a worktree without the setup-token')

    monkeypatch.setattr(cw_session, 'ensure_host_worktree', boom)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 1

  def test_runner_env_points_at_the_scoped_store_registry(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    materialized: dict = {}

    def fake_materialize(store, directory):
      materialized.update(store=store, directory=directory)
      return directory / 'credentials.json'

    monkeypatch.setattr(cw_session, 'materialize_scoped_store', fake_materialize)
    monkeypatch.setattr(
      bro.launch.scope.credentials,
      'build_scoped_store',
      lambda names, optional=(): {'x.cred': b'v'},
    )
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw_session.subprocess, 'run', fake_run)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 0
    registry = tmp_path / 'claude-config' / '.bro' / 'credentials.json'
    assert runs[0][1]['env']['CREDENTIALS_REGISTRY'] == str(registry)
    assert materialized['store'] == {'x.cred': b'v'}
    assert materialized['directory'] == registry.parent

  def test_grant_and_revoke_shape_and_log_the_hydrated_scope(self, monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw_session,
      'scoped_secrets',
      lambda *_a, **_k: ScopedSecrets({'github', 'notion'}, {'openai'}, True),
    )
    hydrated: dict = {}

    def fake_build(names, optional=()):
      hydrated.update(names=set(names), optional=set(optional))
      return {}

    monkeypatch.setattr(bro.launch.scope.credentials, 'build_scoped_store', fake_build)
    monkeypatch.setattr(
      cw_session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    spec = _spec(host=True, grant=['gmail_creds'], revoke=['notion'])
    with caplog.at_level('INFO'):
      assert cw_session._host_session(spec, workspace, None) == 0
    assert hydrated == {
      'names': {'github', 'gmail_creds'},
      'optional': {'openai'},
    }
    assert 'scoped secrets for w: github, gmail_creds' in caplog.text
    assert 'optional (best-effort) secrets for w: openai' in caplog.text

  def test_unresolvable_secret_fails_before_the_worktree(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)

    def missing(names, optional=()):
      raise cw_session.credentials.SecretNotFound('github')

    monkeypatch.setattr(bro.launch.scope.credentials, 'build_scoped_store', missing)

    def boom(*_a, **_k):
      raise AssertionError('must not ensure a worktree when hydration fails')

    monkeypatch.setattr(cw_session, 'ensure_host_worktree', boom)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 1

  def test_missing_inner_cw_fails_before_spawn(self, monkeypatch, tmp_path):
    workspace, worktree = self._fake_workspace(tmp_path, has_session=False)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(cw_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw_session, 'workspace_projects_dir', lambda ws: ws.claude_projects_dir())
    monkeypatch.setattr(cw_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw_session, 'provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw_session.credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      cw_session, '_provision_host_claude_dir', lambda name, wt, project: tmp_path / 'claude-config'
    )
    monkeypatch.setattr(
      cw_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())

    def boom(*_a, **_k):
      raise AssertionError('must not spawn without the inner cw')

    monkeypatch.setattr(cw_session.subprocess, 'run', boom)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 1

  def test_resume_guard_fails_fast_before_worktree_create(self, monkeypatch, tmp_path):
    workspace, _ = self._fake_workspace(tmp_path, has_session=False)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(cw_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw_session, 'workspace_projects_dir', lambda ws: ws.claude_projects_dir())

    def boom(*_a, **_k):
      raise AssertionError('must not create a worktree for a resume with no session')

    monkeypatch.setattr(cw_session, 'ensure_host_worktree', boom)
    assert cw_session._host_session(_spec(host=True, resume=True), workspace, None) == 1


class TestHostBrokerPingRoundTrip:
  """the host broker channel, live: the runner process a host session spawns gets a
  provisioned channel in its env, and `broker request ping` receives a correlated
  pong over it — real socket, real broker loop, real CLI subprocess. Only the
  worktree git plumbing is stubbed (covered by its own tests); the claude-state
  provisioning runs for real against a fake HOME, so the test neither depends on
  the machine's claude login nor writes into the real ~/.claude."""

  def test_broker_request_ping_from_a_host_session(self, monkeypatch, capfd, socket_dir):
    # socket_dir doubles as the project root: the channel socket lands at
    # <project>/var/cw/broker/<channel>.sock
    root = socket_dir
    home = root / 'home'
    home.mkdir()
    # the identity fields _seed_claude_json requires from the host ~/.claude.json
    (home / '.claude.json').write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'u'}))
    monkeypatch.setenv('HOME', str(home))
    worktree = root / 'wt'
    cw_bin = worktree / '.venv' / 'bin' / 'cw'
    cw_bin.parent.mkdir(parents=True)
    # stands in for the in-place runner: the real `broker` CLI resolves from the
    # ambient venv PATH (retained through _venv_env) and rides BROKER_CHANNEL
    cw_bin.write_text('#!/bin/sh\nexec broker request ping "{}" --timeout 30\n')
    cw_bin.chmod(0o755)

    class _FakeHost(cw_session.HostWorktree):
      @property
      def path(self):
        return worktree

    workspace = _FakeHost('w', root)
    monkeypatch.setattr(cw_session, 'project_root', lambda: root)
    monkeypatch.setattr(cw_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw_session, 'provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(bro.launch.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    monkeypatch.setattr(cw_session.credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      cw_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set(), True)
    )
    monkeypatch.setattr(
      bro.launch.scope.credentials, 'build_scoped_store', lambda names, optional=(): {}
    )
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    assert cw_session._host_session(_spec(host=True), workspace, None) == 0
    # the CLI printed the correlated reply's wire JSON
    assert '"pong"' in capfd.readouterr().out
    # the channel socket is unlinked once the root exits
    assert list((root / 'var' / 'cw' / 'broker').glob('*.sock')) == []
    # the session claude state landed under the fake HOME, seeded from its identity
    seeded = home / '.claude' / 'cw-sessions' / 'w' / '.claude.json'
    assert json.loads(seeded.read_text())['userID'] == 'u'
