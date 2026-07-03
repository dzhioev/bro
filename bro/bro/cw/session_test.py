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
  container: bool = True,
  drop: bool = False,
  auto: bool = False,
  fast: bool = False,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
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
    container=container,
    drop=drop,
    auto=auto,
    fast=fast,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
    effort=effort,
    resume=resume,
    into=into,
    mcp=mcp,
    bro=bro,
    prompt=prompt,
    claude_args=claude_args if claude_args is not None else [],
  )


class TestResolveBaseRef:
  def _patch(self, monkeypatch, *, local_rc, fetch_rc=1, fetched_sha='deadbeef'):
    from types import SimpleNamespace

    monkeypatch.setattr(cw.session, '_project_root', lambda: pathlib.Path('/repo'))
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

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    return calls

  def test_resolves_host_local_ref_without_fetching(self, monkeypatch):
    calls = self._patch(monkeypatch, local_rc=0)
    assert cw.session._resolve_base_ref('master') == 'localsha'
    assert not any(c[:2] == ['git', 'fetch'] for c in calls)

  def test_fetches_origin_when_ref_not_host_local(self, monkeypatch):
    calls = self._patch(monkeypatch, local_rc=1, fetch_rc=0, fetched_sha='abc123')
    assert cw.session._resolve_base_ref('worktree-feature') == 'abc123'
    assert ['git', 'fetch', 'origin', 'worktree-feature'] in calls

  def test_returns_none_when_neither_resolves(self, monkeypatch):
    self._patch(monkeypatch, local_rc=1, fetch_rc=1)
    assert cw.session._resolve_base_ref('nope') is None


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
        'cw.session._container_secrets',
        return_value=ScopedSecrets(set(self.secrets), set(), True),
      ),
      patch('cw.session._replace_container_resume_hint'),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('CW_BRO', None)
    self.env.pop('CW_IN_CONTAINER', None)
    self.run_in_container = entered[2]
    return self

  def __exit__(self, *exc):
    for p in reversed(self._patches):
      p.__exit__(*exc)
    return False


class TestGrantRevoke:
  def test_start_session_applies_grant_and_revoke(self):
    with _ContainerHarness(secrets={'notion', 'trails', 'github'}) as h:
      rc = cw.session.start_session(_spec(drop=True, grant=['gmail_creds'], revoke=['notion']))
    assert rc == 0
    _, kwargs = h.run_in_container.call_args
    assert 'gmail_creds' in kwargs['secrets']
    assert 'notion' not in kwargs['secrets']

  def test_start_session_grant_already_present_returns_1(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(drop=True, grant=['github']))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_start_session_injects_effort_into_the_container_command(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(drop=True, effort='xhigh'))
    assert rc == 0
    command = h.run_in_container.call_args[0][1]
    assert command[command.index('--effort') + 1] == 'xhigh'


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

  def test_no_extra_env_without_into(self):
    with _ContainerHarness() as h:
      rc = cw.session.start_session(_spec(mcp=None, drop=True))
    assert rc == 0
    _, kwargs = h.run_in_container.call_args
    assert kwargs['extra_env'] is None

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
      container=True,
      auto=True,
      fast=True,
      drop=True,
      effort='xhigh',
      mcp='http',
      grant=['gmail_creds'],
      revoke=['notion'],
      into='feature',
      claude_args=['--foo'],
    ).to_command_argv()
    assert parts == [
      'cw', 'ss', '-c', '--auto', '--fast', '--drop',
      '--effort', 'xhigh', '--mcp=http', '--grant', 'gmail_creds',
      '--revoke', 'notion', '--into', 'feature', 'w', '--foo',
    ]  # fmt: skip

  def test_resume_variant_carries_forwarded_flags_and_clears_create_only(self):
    # resume_variant keeps --auto/--effort/--mcp/--grant and adds --resume, while
    # clearing the create-only --drop/--into/prompt/claude args
    parts = (
      _spec(
        container=True,
        auto=True,
        drop=True,
        effort='xhigh',
        mcp='http',
        grant=['gmail_creds'],
        into='feature',
        prompt='do it',
        claude_args=['--foo'],
      )
      .resume_variant()
      .to_command_argv()
    )
    assert parts == [
      'cw', 'ss', '-c', '--auto', '--resume',
      '--effort', 'xhigh', '--mcp=http', '--grant', 'gmail_creds', 'w',
    ]  # fmt: skip

  def test_start_session_records_resume_command(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session._container_session', return_value=0),
    ):
      env.pop('CW_IN_CONTAINER', None)
      cw.session.start_session(
        _spec(
          container=True, drop=True, auto=True, grant=['gmail_creds'], effort='xhigh', mcp='http'
        )
      )
      resume_command = env['CW_RESUME_COMMAND']
    assert (
      resume_command == 'cw ss -c --auto --resume --effort xhigh --mcp=http --grant gmail_creds w'
    )


class TestInPlaceArgv:
  def test_drops_machinery_flags_and_carries_the_rest(self):
    parts = _spec(
      container=True,
      auto=True,
      fast=True,
      drop=True,
      effort='xhigh',
      mcp='local',
      grant=['gmail_creds'],
      revoke=['notion'],
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
    assert cw.session._container_session(_spec(container=True), None) == 1

  def test_container_proceeds_when_inactive(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session, 'find_container_id', lambda path: None)
    monkeypatch.setattr(
      cw.session,
      '_container_secrets',
      lambda *_a, **_k: ScopedSecrets(set(), set(), True),
    )
    called: list = []
    monkeypatch.setattr(cw.session, 'run_in_container', lambda *_a, **_k: called.append(True) or 0)
    monkeypatch.setattr(cw.session, '_replace_container_resume_hint', lambda name: None)
    assert cw.session._container_session(_spec(container=True), None) == 0
    assert called == [True]

  def test_host_refuses_when_active(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)

    class _FakeHost:
      def __init__(self, name, proj):
        self.path = pathlib.Path('/wt')
        self.pidfile = pathlib.Path('/wt.pid')

      def is_active(self, mounts):
        return True

    monkeypatch.setattr(cw.session, 'HostWorktree', _FakeHost)

    def boom(*_a, **_k):
      raise AssertionError('must not provision when a session is already active')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    assert cw.session._host_session(_spec(container=False), None) == 1

  def test_host_proceeds_when_inactive(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)

    class _FakeHost:
      def __init__(self, name, proj):
        self.path = pathlib.Path('/wt')
        self.pidfile = pathlib.Path('/wt.pid')

      def is_active(self, mounts):
        return False

    monkeypatch.setattr(cw.session, 'HostWorktree', _FakeHost)
    called: list = []
    monkeypatch.setattr(
      cw.session, '_ensure_host_worktree', lambda *_a: called.append(True) or False
    )
    assert cw.session._host_session(_spec(container=False), None) == 1
    assert called == [True]


class TestHostSession:
  def _fake_host(self, tmp_path, *, has_session: bool):
    worktree = tmp_path / 'wt'
    projects = tmp_path / 'projects'
    projects.mkdir()
    if has_session:
      (projects / 'abc.jsonl').write_text('{}')

    class _FakeHost:
      def __init__(self, name, proj):
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
    return cw_bin, worktree

  def test_broker_supervises_the_worktrees_own_in_place_runner(self, monkeypatch, tmp_path):
    cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: True)
    roots: list = []

    def fake_root(command, worktree_arg, proj):
      roots.append({'command': command, 'worktree': worktree_arg, 'proj': proj})
      return 5

    monkeypatch.setattr(cw.session, '_run_host_root_via_broker', fake_root)
    spec = _spec(container=False, auto=True, effort='xhigh', prompt='go', claude_args=['--foo'])
    assert cw.session._host_session(spec, None) == 5
    assert roots == [
      {
        'command': [
          str(cw_bin),
          'ss',
          '--in-place',
          '--auto',
          '--effort',
          'xhigh',
          '--prompt=go',
          'w',
          '--foo',
        ],  # fmt: skip
        'worktree': worktree,
        'proj': tmp_path,
      }
    ]

  def test_direct_spawn_when_broker_disabled(self, monkeypatch, tmp_path):
    cw_bin, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cw.session, '_broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    spec = _spec(container=False, auto=True, effort='xhigh', prompt='go', claude_args=['--foo'])
    assert cw.session._host_session(spec, None) == 0
    argv, kwargs = runs[0]
    assert argv == [
      str(cw_bin), 'ss', '--in-place', '--auto', '--effort', 'xhigh', '--prompt=go', 'w', '--foo',
    ]  # fmt: skip
    assert kwargs['cwd'] == str(worktree)
    assert kwargs['env']['VIRTUAL_ENV'] == str(worktree / '.venv')

  def test_missing_inner_cw_fails_before_spawn(self, monkeypatch, tmp_path):
    fake_host, worktree = self._fake_host(tmp_path, has_session=False)
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw.session, 'HostWorktree', fake_host)
    monkeypatch.setattr(cw.session, '_ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(cw.session, '_provision_host_worktree', lambda *_a: True)

    def boom(*_a, **_k):
      raise AssertionError('must not spawn without the inner cw')

    monkeypatch.setattr(cw.session.subprocess, 'run', boom)
    assert cw.session._host_session(_spec(container=False), None) == 1

  def test_resume_guard_fails_fast_before_worktree_create(self, monkeypatch, tmp_path):
    fake_host, _ = self._fake_host(tmp_path, has_session=False)
    monkeypatch.setattr(cw.session, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(cw.session, 'HostWorktree', fake_host)

    def boom(*_a, **_k):
      raise AssertionError('must not create a worktree for a resume with no session')

    monkeypatch.setattr(cw.session, '_ensure_host_worktree', boom)
    assert cw.session._host_session(_spec(container=False, resume=True), None) == 1


class TestHostBrokerPingRoundTrip:
  """the host broker channel, live: the runner process a host session spawns gets a
  provisioned channel in its env, and `broker request ping` receives a correlated
  pong over it — real socket, real broker loop, real CLI subprocess. Only the
  worktree git plumbing is stubbed (covered by its own tests)."""

  def test_broker_request_ping_from_a_host_session(self, monkeypatch, capfd):
    # a short root directly under the system temp dir: the channel socket lives at
    # <proj>/var/cw/broker/<ulid>.sock and must fit sun_path (~108 bytes), which a
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
        def __init__(self, name, proj):
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
      monkeypatch.delenv('BROKER_DISABLED', raising=False)
      assert cw.session._host_session(_spec(container=False), None) == 0
      # the CLI printed the correlated reply's wire JSON
      assert '"pong"' in capfd.readouterr().out
      # the channel socket is unlinked once the root exits
      assert list((root / 'var' / 'cw' / 'broker').glob('*.sock')) == []
    finally:
      shutil.rmtree(root, ignore_errors=True)
