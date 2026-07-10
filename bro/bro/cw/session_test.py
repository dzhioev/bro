import pathlib
import shutil
import tempfile
from typing import Optional
from unittest.mock import patch

import cw.session
from cw.secrets import ScopedSecrets


def _spec(
  *,
  name: str = 'w',
  host: bool = False,
  drop: bool = False,
  auto: bool = False,
  fast: bool = False,
  grant_cred: Optional[list[str]] = None,
  revoke_cred: Optional[list[str]] = None,
  grant_summon: Optional[list[str]] = None,
  revoke_summon: Optional[list[str]] = None,
  effort: Optional[str] = None,
  resume: bool = False,
  into: Optional[str] = None,
  mcp: Optional[str] = None,
  bro: Optional[str] = None,
  prompt: Optional[str] = None,
  claude_args: Optional[list[str]] = None,
) -> cw.session.SessionSpec:
  return cw.session.SessionSpec(
    name=name,
    host=host,
    drop=drop,
    auto=auto,
    fast=fast,
    grant_cred=grant_cred if grant_cred is not None else [],
    revoke_cred=revoke_cred if revoke_cred is not None else [],
    grant_summon=grant_summon if grant_summon is not None else [],
    revoke_summon=revoke_summon if revoke_summon is not None else [],
    effort=effort,
    resume=resume,
    into=into,
    mcp=mcp,
    bro=bro,
    prompt=prompt,
    claude_args=claude_args if claude_args is not None else [],
  )


class _ContainerHarness:
  """patches for driving start_session through the container path without docker,
  bro imports, or git side effects."""

  def __init__(self, secrets: Optional[set[str]] = None):
    self.secrets = secrets if secrets is not None else {'github'}

  def __enter__(self):
    self._patches = [
      patch.dict('os.environ', {}, clear=False),
      patch('cw.session.find_container_id', return_value=None),
      patch('cw.session.run_in_container', return_value=0),
      patch(
        'cw.session._session_secrets',
        return_value=ScopedSecrets(set(self.secrets), set(), True),
      ),
      patch('cw.session._replace_resume_hint'),
      # keep the bro-registry import out; threading is asserted per-test
      patch('cw.session.summon_allow_list', return_value=set()),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('CW_BRO', None)
    self.env.pop('CW_IN_CONTAINER', None)
    self.run_in_container = entered[2]
    self.summon_allow_list = entered[5]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestGrantRevoke:
  def test_start_session_applies_grant_and_revoke(self):
    with _ContainerHarness(secrets={'notion', 'trails', 'github'}) as h:
      rc = cw.session.start_session(
        _spec(drop=True, grant_cred=['gmail_creds'], revoke_cred=['notion'])
      )
    assert rc == 0
    _, kwargs = h.run_in_container.call_args
    assert 'gmail_creds' in kwargs['secrets']
    assert 'notion' not in kwargs['secrets']

  def test_start_session_grant_already_present_returns_1(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(drop=True, grant_cred=['github']))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_start_session_injects_effort_into_the_container_command(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(drop=True, effort='xhigh'))
    assert rc == 0
    command = h.run_in_container.call_args[0][1]
    assert command[command.index('--effort') + 1] == 'xhigh'


class TestSummonAllowList:
  def test_container_session_threads_the_allow_list(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.return_value = {'devoops'}
      rc = cw.session.start_session(_spec(drop=True, grant_summon=['devoops']))
    assert rc == 0
    # identity: no --bro and no ambient CW_BRO → the ppp-dev default
    assert h.summon_allow_list.call_args == (
      ('ppp-dev',),
      {'grant': ['devoops'], 'revoke': []},
    )
    _, kwargs = h.run_in_container.call_args
    assert kwargs['may_summon'] == {'devoops'}

  def test_container_session_keys_identity_on_the_bro(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(drop=True, bro='pm'))
    assert rc == 0
    assert h.summon_allow_list.call_args[0] == ('pm',)

  def test_bad_summon_flag_fails_the_launch(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.side_effect = ValueError('unknown summon target(s): devoop')
      rc = cw.session.start_session(_spec(drop=True, grant_summon=['devoop']))
    assert rc == 1
    assert h.run_in_container.call_count == 0


class TestContainerCommand:
  def test_command_is_the_in_place_invocation(self):
    # the docker command is the same in-place runner host mode spawns; the
    # argv/MCP/skills work happens inside the container, next to claude
    with _ContainerHarness() as h:
      rc = cw.session.start_session(
        _spec(drop=True, fast=True, mcp='local', effort='xhigh', prompt='go')
      )
    assert rc == 0
    command = h.run_in_container.call_args[0][1]
    assert command == [
      'cw', 'ss', '--in-place', '--fast', '--effort', 'xhigh', '--mcp=local', '--prompt=go', 'w',
    ]  # fmt: skip

  def test_bro_carried_in_command_and_cw_bro_forwarded(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(drop=True, bro='pm'))
      # CW_BRO themes the container beyond the runner's process tree (cw exec)
      forwarded_bro = h.env.get('CW_BRO')
    assert rc == 0
    command = h.run_in_container.call_args[0][1]
    assert command == ['cw', 'ss', '--in-place', '--bro', 'pm', 'w']
    assert forwarded_bro == 'pm'

  def test_default_base_is_left_to_the_entrypoint_head_fallback(self):
    # no CW_BASE_REF by default: the clone bases on HEAD — the host checkout as
    # cloned — with no network touched on the way
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(mcp=None, drop=True))
    assert rc == 0
    _, kwargs = h.run_in_container.call_args
    assert kwargs['extra_env'] is None

  def test_into_threads_the_resolved_base_into_the_container_env(self):
    with _ContainerHarness() as h:
      with patch('cw.session.resolve_ref', return_value='intosha') as resolve:
        rc = cw.session.start_session(_spec(mcp=None, drop=True, into='feature'))
    assert rc == 0
    assert resolve.call_args[0][1] == 'feature'
    _, kwargs = h.run_in_container.call_args
    assert kwargs['extra_env'] == {'CW_BASE_REF': 'intosha'}

  def test_unresolvable_into_fails_launch(self):
    with _ContainerHarness() as h:
      with patch('cw.session.resolve_ref', return_value=None):
        rc = cw.session.start_session(_spec(mcp=None, drop=True, into='nope'))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_guard_fails_fast_without_a_session(self, tmp_path):
    with _ContainerHarness() as h:
      with patch.object(cw.session.ContainerWorkspace, 'claude_projects_dir') as projects:
        projects.return_value = tmp_path
        rc = cw.session.start_session(_spec(resume=True))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_carried_as_bare_flag_the_runner_resolves(self, tmp_path):
    (tmp_path / 'abc.jsonl').write_text('{}')
    with _ContainerHarness() as h:
      with patch.object(cw.session.ContainerWorkspace, 'claude_projects_dir') as projects:
        projects.return_value = tmp_path
        rc = cw.session.start_session(_spec(resume=True))
    assert rc == 0
    command = h.run_in_container.call_args[0][1]
    assert command == ['cw', 'ss', '--in-place', '--resume', 'w']


class TestResumeCommand:
  def test_create_command_includes_drop_into_and_claude_args(self):
    parts = _spec(
      auto=True,
      fast=True,
      drop=True,
      effort='xhigh',
      mcp='http',
      grant_cred=['gmail_creds'],
      revoke_cred=['notion'],
      into='feature',
      claude_args=['--foo'],
    ).to_command_argv()
    assert parts == [
      'cw', 'ss', '--auto', '--fast', '--drop',
      '--effort', 'xhigh', '--mcp=http', '--grant-cred', 'gmail_creds',
      '--revoke-cred', 'notion', '--into', 'feature', 'w', '--foo',
    ]  # fmt: skip

  def test_host_session_carries_the_host_flag(self):
    parts = _spec(host=True, auto=True).to_command_argv()
    assert parts == ['cw', 'ss', '--host', '--auto', 'w']

  def test_resume_variant_carries_forwarded_flags_and_clears_create_only(self):
    # resume_variant keeps --auto/--effort/--mcp/--grant-cred and adds --resume,
    # while clearing the create-only --drop/--into/prompt/claude args
    parts = (
      _spec(
        auto=True,
        drop=True,
        effort='xhigh',
        mcp='http',
        grant_cred=['gmail_creds'],
        into='feature',
        prompt='do it',
        claude_args=['--foo'],
      )
      .resume_variant()
      .to_command_argv()
    )
    assert parts == [
      'cw', 'ss', '--auto', '--resume',
      '--effort', 'xhigh', '--mcp=http', '--grant-cred', 'gmail_creds', 'w',
    ]  # fmt: skip

  def test_start_session_records_resume_command(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session._container_session', return_value=0),
    ):
      env.pop('CW_IN_CONTAINER', None)
      cw.session.start_session(
        _spec(
          drop=True,
          auto=True,
          grant_cred=['gmail_creds'],
          effort='xhigh',
          mcp='http',
        )
      )
      resume_command = env['CW_RESUME_COMMAND']
    assert (
      resume_command == 'cw ss --auto --resume --effort xhigh --mcp=http --grant-cred gmail_creds w'
    )


class TestReplaceResumeHint:
  def test_prints_recorded_command_over_claudes_hint(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv('CW_RESUME_COMMAND', 'cw ss --auto --resume w')
    monkeypatch.setattr(cw.session, '_latest_jsonl', lambda directory: 'session.jsonl')
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    cw.session._replace_resume_hint(cw.session.HostWorktree('w', tmp_path))
    assert 'cw ss --auto --resume w' in capsys.readouterr().out

  def test_silent_without_a_session_jsonl(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv('CW_RESUME_COMMAND', 'cw ss --resume w')
    monkeypatch.setattr(cw.session, '_latest_jsonl', lambda directory: None)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    cw.session._replace_resume_hint(cw.session.HostWorktree('w', tmp_path))
    assert capsys.readouterr().out == ''


class TestInPlaceArgv:
  def test_drops_machinery_flags_and_carries_the_rest(self):
    parts = _spec(
      host=True,
      auto=True,
      fast=True,
      drop=True,
      effort='xhigh',
      mcp='local',
      grant_cred=['gmail_creds'],
      revoke_cred=['notion'],
      into='feature',
      prompt='do it',
      claude_args=['--foo'],
    ).to_in_place_argv()
    assert parts == [
      'ss', '--in-place', '--auto', '--fast',
      '--effort', 'xhigh', '--mcp=local', '--prompt=do it', 'w', '--foo',
    ]  # fmt: skip

  def test_resume_and_bro_carried(self):
    parts = _spec(resume=True, bro='pm').to_in_place_argv()
    assert parts == ['ss', '--in-place', '--resume', '--bro', 'pm', 'w']

  def test_mcp_joined_so_the_name_cannot_be_its_value(self):
    # a bare `--mcp` directly followed by the name would make the nargs='?'
    # parser swallow the name as the choice value
    parts = _spec(mcp='http').to_in_place_argv()
    assert parts == ['ss', '--in-place', '--mcp=http', 'w']


class TestConcurrentSessionGuard:
  def test_container_refuses_when_active(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session, 'find_container_id', lambda path: 'abc123')

    def boom(*_a, **_k):
      raise AssertionError('must not launch a second container session')

    monkeypatch.setattr(cw.session, 'run_in_container', boom)
    assert cw.session._container_session(_spec(), None) == 1

  def test_container_proceeds_when_inactive(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session, 'find_container_id', lambda path: None)
    monkeypatch.setattr(
      cw.session,
      '_session_secrets',
      lambda *_a, **_k: ScopedSecrets(set(), set(), True),
    )
    monkeypatch.setattr(cw.session, 'summon_allow_list', lambda *_a, **_k: set())
    called: list = []
    monkeypatch.setattr(cw.session, 'run_in_container', lambda *_a, **_k: called.append(True) or 0)
    monkeypatch.setattr(cw.session, '_replace_resume_hint', lambda workspace: None)
    assert cw.session._container_session(_spec(), None) == 0
    assert called == [True]

  def test_host_refuses_when_active(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)

    class _FakeHost:
      def __init__(self, name, project):
        self.path = pathlib.Path('/wt')
        self.pidfile = pathlib.Path('/wt.pid')

      def is_active(self, mounts):
        return True

    monkeypatch.setattr(cw.session, 'HostWorktree', _FakeHost)

    def boom(*_a, **_k):
      raise AssertionError('must not provision when a session is already active')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    assert cw.session._host_session(_spec(host=True), None) == 1

  def test_host_proceeds_when_inactive(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)

    class _FakeHost:
      def __init__(self, name, project):
        self.path = pathlib.Path('/wt')
        self.pidfile = pathlib.Path('/wt.pid')

      def is_active(self, mounts):
        return False

    monkeypatch.setattr(cw.session, 'HostWorktree', _FakeHost)
    monkeypatch.setattr(cw.session, 'summon_allow_list', lambda *_a, **_k: set())
    called: list = []
    monkeypatch.setattr(
      cw.session, '_ensure_host_worktree', lambda *_a: called.append(True) or False
    )
    assert cw.session._host_session(_spec(host=True), None) == 1
    assert called == [True]


class TestHostSession:
  def _fake_host(self, tmp_path, *, has_session: bool):
    worktree = tmp_path / 'wt'
    projects = tmp_path / 'projects'
    projects.mkdir()
    if has_session:
      (projects / 'abc.jsonl').write_text('{}')

    class _FakeHost:
      def __init__(self, name, project):
        self.path = worktree
        self.pidfile = tmp_path / 'wt.pid'

      def is_active(self, mounts):
        return False

      def claude_projects_dir(self):
        return projects

      def remove(self):
        pass

    return _FakeHost, worktree

  def _prepare_launch(self, monkeypatch, tmp_path):
    fake_host, worktree = self._fake_host(tmp_path, has_session=False)
    cw_bin = worktree / '.venv' / 'bin' / 'cw'
    cw_bin.parent.mkdir(parents=True)
    cw_bin.write_text('')
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw.session, 'HostWorktree', fake_host)
    monkeypatch.setattr(cw.session, '_ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw.session, '_provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw.session, '_finish_host_worktree', lambda *_a, **_k: None)
    # keep the launch tests off the real credential store; the auth-transform
    # test overrides this with its own fake
    monkeypatch.setattr(cw.session, '_apply_claude_auth', lambda env, **_k: None)
    monkeypatch.setattr(cw.session.credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      cw.session, '_provision_host_claude_dir', lambda name, wt, project: tmp_path / 'claude-config'
    )
    monkeypatch.setattr(
      cw.session, '_session_secrets', lambda *_a, **_k: ScopedSecrets({'github'}, set(), True)
    )
    monkeypatch.setattr(cw.session.credentials, 'build_scoped_store', lambda names, optional=(): {})
    monkeypatch.setattr(
      cw.session,
      '_materialize_scoped_store',
      lambda store, directory: directory / 'credentials.json',
    )
    monkeypatch.setattr(cw.session, 'summon_allow_list', lambda *_a, **_k: set())
    return cw_bin, worktree

  def test_broker_supervises_the_worktrees_own_in_place_runner(self, monkeypatch, tmp_path):
    cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: True)
    monkeypatch.setattr(cw.session, 'summon_allow_list', lambda *_a, **_k: {'devoops'})
    roots: list = []

    def fake_root(name, command, worktree_arg, project, env, may_summon):
      roots.append(
        {
          'name': name,
          'command': command,
          'worktree': worktree_arg,
          'project': project,
          'env': env,
          'may_summon': may_summon,
        }
      )
      return 5

    monkeypatch.setattr(cw.session, '_run_host_root_via_broker', fake_root)
    spec = _spec(host=True, auto=True, effort='xhigh', prompt='go', claude_args=['--foo'])
    assert cw.session._host_session(spec, None) == 5
    assert roots[0]['name'] == 'w'
    assert roots[0]['command'] == [
      str(cw_bin), 'ss', '--in-place', '--auto', '--effort', 'xhigh', '--prompt=go', 'w', '--foo',
    ]  # fmt: skip
    assert roots[0]['worktree'] == worktree
    assert roots[0]['project'] == tmp_path
    assert roots[0]['env']['VIRTUAL_ENV'] == str(worktree / '.venv')
    # the host root gets the session's summon allow-list like container mode
    assert roots[0]['may_summon'] == {'devoops'}

  def test_bad_summon_flag_fails_before_the_worktree_is_ensured(self, monkeypatch, tmp_path):
    self._prepare_launch(monkeypatch, tmp_path)

    def bad_allow_list(*_a, **_k):
      raise ValueError('unknown summon target(s): devoop')

    monkeypatch.setattr(cw.session, 'summon_allow_list', bad_allow_list)

    def boom(*_a, **_k):
      raise AssertionError('must not ensure a worktree when the summon flags are bad')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    spec = _spec(host=True, grant_summon=['devoop'])
    assert cw.session._host_session(spec, None) == 1

  def test_direct_spawn_when_broker_disabled(self, monkeypatch, tmp_path):
    cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    spec = _spec(host=True, auto=True, effort='xhigh', prompt='go', claude_args=['--foo'])
    assert cw.session._host_session(spec, None) == 0
    argv, kwargs = runs[0]
    assert argv == [
      str(cw_bin), 'ss', '--in-place', '--auto', '--effort', 'xhigh', '--prompt=go', 'w', '--foo',
    ]  # fmt: skip
    assert kwargs['cwd'] == str(worktree)
    assert kwargs['env']['VIRTUAL_ENV'] == str(worktree / '.venv')

  def test_resume_hint_precedes_the_keep_or_drop_offer(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw.session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    events: list = []
    monkeypatch.setattr(cw.session, '_replace_resume_hint', lambda workspace: events.append('hint'))
    monkeypatch.setattr(
      cw.session, '_finish_host_worktree', lambda *_a, **_k: events.append('finish')
    )
    assert cw.session._host_session(_spec(host=True), None) == 0
    assert events == ['hint', 'finish']

  def test_resume_hint_skipped_when_the_session_failed(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw.session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=3)
    )
    events: list = []
    monkeypatch.setattr(cw.session, '_replace_resume_hint', lambda workspace: events.append('hint'))
    monkeypatch.setattr(
      cw.session, '_finish_host_worktree', lambda *_a, **_k: events.append('finish')
    )
    assert cw.session._host_session(_spec(host=True), None) == 3
    assert events == ['finish']

  def test_runner_env_gets_the_claude_auth_transform(self, monkeypatch, tmp_path):
    # the outer applies _apply_claude_auth to the runner env it spawns, so a
    # worktree whose own runner predates the transform still inherits the token
    cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)

    def fake_apply(env, **_kwargs):
      env['CLAUDE_CODE_OAUTH_TOKEN'] = 'applied'

    monkeypatch.setattr(cw.session, '_apply_claude_auth', fake_apply)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    assert cw.session._host_session(_spec(host=True), None) == 0
    assert runs[0][1]['env']['CLAUDE_CODE_OAUTH_TOKEN'] == 'applied'

  def test_runner_env_points_at_the_private_claude_config_dir(self, monkeypatch, tmp_path):
    # the outer provisions the per-session state dir and exports CLAUDE_CONFIG_DIR
    # itself, so a worktree whose own runner predates the config dir is covered too
    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    assert cw.session._host_session(_spec(host=True), None) == 0
    assert runs[0][1]['env']['CLAUDE_CONFIG_DIR'] == str(tmp_path / 'claude-config')

  def test_missing_claude_code_fails_a_native_launch_before_the_worktree(
    self, monkeypatch, tmp_path
  ):
    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session.credentials, 'try_get', lambda name: None)

    def boom(*_a, **_k):
      raise AssertionError('must not ensure a worktree without the setup-token')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    assert cw.session._host_session(_spec(host=True), None) == 1

  def test_missing_claude_code_does_not_gate_a_bro_launch(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session.credentials, 'try_get', lambda name: None)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw.session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    assert cw.session._host_session(_spec(host=True, bro='devoops'), None) == 0

  def test_runner_env_points_at_the_scoped_store_registry(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    materialized: dict = {}

    def fake_materialize(store, directory):
      materialized.update(store=store, directory=directory)
      return directory / 'credentials.json'

    monkeypatch.setattr(cw.session, '_materialize_scoped_store', fake_materialize)
    monkeypatch.setattr(
      cw.session.credentials, 'build_scoped_store', lambda names, optional=(): {'x.cred': b'v'}
    )
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    assert cw.session._host_session(_spec(host=True), None) == 0
    registry = tmp_path / 'claude-config' / '.ppp' / 'credentials.json'
    assert runs[0][1]['env']['CREDENTIALS_REGISTRY'] == str(registry)
    assert materialized['store'] == {'x.cred': b'v'}
    assert materialized['directory'] == registry.parent

  def test_grant_and_revoke_shape_the_hydrated_set(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    monkeypatch.setattr(
      cw.session,
      '_session_secrets',
      lambda *_a, **_k: ScopedSecrets({'github', 'notion'}, set(), True),
    )
    hydrated: dict = {}

    def fake_build(names, optional=()):
      hydrated.update(names=set(names), optional=set(optional))
      return {}

    monkeypatch.setattr(cw.session.credentials, 'build_scoped_store', fake_build)
    monkeypatch.setattr(
      cw.session.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0)
    )
    spec = _spec(host=True, grant_cred=['gmail_creds'], revoke_cred=['notion'])
    assert cw.session._host_session(spec, None) == 0
    assert hydrated['names'] == {'github', 'gmail_creds'}

  def test_unresolvable_secret_fails_before_the_worktree(self, monkeypatch, tmp_path):
    self._prepare_launch(monkeypatch, tmp_path)

    def missing(names, optional=()):
      raise cw.session.credentials.SecretNotFound('github')

    monkeypatch.setattr(cw.session.credentials, 'build_scoped_store', missing)

    def boom(*_a, **_k):
      raise AssertionError('must not ensure a worktree when hydration fails')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    assert cw.session._host_session(_spec(host=True), None) == 1

  def test_missing_inner_cw_fails_before_spawn(self, monkeypatch, tmp_path):
    fake_host, worktree = self._fake_host(tmp_path, has_session=False)
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw.session, 'HostWorktree', fake_host)
    monkeypatch.setattr(cw.session, '_ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw.session, '_provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw.session.credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      cw.session, '_provision_host_claude_dir', lambda name, wt, project: tmp_path / 'claude-config'
    )
    monkeypatch.setattr(
      cw.session, '_session_secrets', lambda *_a, **_k: ScopedSecrets(set(), set(), True)
    )
    monkeypatch.setattr(cw.session.credentials, 'build_scoped_store', lambda names, optional=(): {})
    monkeypatch.setattr(cw.session, 'summon_allow_list', lambda *_a, **_k: set())

    def boom(*_a, **_k):
      raise AssertionError('must not spawn without the inner cw')

    monkeypatch.setattr(cw.session.subprocess, 'run', boom)
    assert cw.session._host_session(_spec(host=True), None) == 1

  def test_resume_guard_fails_fast_before_worktree_create(self, monkeypatch, tmp_path):
    fake_host, _ = self._fake_host(tmp_path, has_session=False)
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw.session, 'HostWorktree', fake_host)

    def boom(*_a, **_k):
      raise AssertionError('must not create a worktree for a resume with no session')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    assert cw.session._host_session(_spec(host=True, resume=True), None) == 1


class TestHostBrokerPingRoundTrip:
  """the host broker channel, live: the runner process a host session spawns gets a
  provisioned channel in its env, and `broker request ping` receives a correlated
  pong over it — real socket, real broker loop, real CLI subprocess. Only the
  worktree git plumbing is stubbed (covered by its own tests)."""

  def test_broker_request_ping_from_a_host_session(self, monkeypatch, capfd):
    # a short root directly under the system temp dir: the channel socket lives at
    # <project>/var/cw/broker/<channel>.sock and must fit sun_path (~108 bytes), which a
    # pytest tmp_path can exceed
    root = pathlib.Path(tempfile.mkdtemp(prefix='cw-hb-'))
    try:
      worktree = root / 'wt'
      cw_bin = worktree / '.venv' / 'bin' / 'cw'
      cw_bin.parent.mkdir(parents=True)
      # stands in for the in-place runner: the real `broker` CLI resolves from the
      # ambient venv PATH (retained through _venv_env) and rides BROKER_CHANNEL
      cw_bin.write_text('#!/bin/sh\nexec broker request ping "{}" --timeout 30\n')
      cw_bin.chmod(0o755)

      class _FakeHost:
        def __init__(self, name, project):
          self.path = worktree
          self.pidfile = root / 'wt.pid'

        def is_active(self, mounts):
          return False

      monkeypatch.setattr(cw.session, '_project_root', lambda: root)
      monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)
      monkeypatch.setattr(cw.session, 'HostWorktree', _FakeHost)
      monkeypatch.setattr(cw.session, '_ensure_host_worktree', lambda *_a: True)
      monkeypatch.setattr(cw.session, '_provision_host_worktree', lambda *_a: True)
      monkeypatch.setattr(cw.session, '_finish_host_worktree', lambda *_a, **_k: None)
      monkeypatch.setattr(cw.session, 'summon_allow_list', lambda *_a, **_k: set())
      monkeypatch.delenv('BROKER_DISABLED', raising=False)
      assert cw.session._host_session(_spec(host=True), None) == 0
      # the CLI printed the correlated reply's wire JSON
      assert '"pong"' in capfd.readouterr().out
      # the channel socket is unlinked once the root exits
      assert list((root / 'var' / 'cw' / 'broker').glob('*.sock')) == []
    finally:
      shutil.rmtree(root, ignore_errors=True)
