import contextlib
import json
import os
import shlex
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

import bro.summon
import bro.workspace.project as workspace_project
import ride.claude.harness as claude_harness
import ride.scope
import ride.session as ride_session
import ride.spawn
import ride.summon_control
from bro.base import credentials
from bro.monitor import workspace_session_dir
from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV
from bro.workspace.paths import CONTAINER_SESSION_DIR
from ride import pending_summon
from ride.repository import Repository
from ride.runtime_bundle import RuntimeBundle, RuntimeBundleError
from ride.scope import ScopedSecrets
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace
from ride.workspace.store import materialize_scoped_store


@pytest.fixture(autouse=True)
def isolated_environ():
  """start_session exports session facts (RIDE_COMMAND, RIDE_WORKSPACE,
  BRO_SHELL_COMMAND) into the live process environment; snapshot-restore it so
  no test here leaks them into the rest of the suite."""
  with patch.dict(os.environ, {}, clear=False):
    yield


def _spec(
  *,
  name: str = 'w',
  host: bool = False,
  drop: bool = False,
  no_trails: bool = False,
  hold: str = 'attended',
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  llm: Optional[str] = None,
  solo: bool = False,
  resume: bool = False,
  into: Optional[str] = None,
  bro: Optional[str] = None,
  raw: bool = False,
  prompt: Optional[str] = None,
  arguments: Optional[list[str]] = None,
) -> ride_session.SessionSpec:
  from ride.claude.harness import ClaudeOptions

  resolved_bro = bro if bro is not None else 'bro-dev'
  return ride_session.SessionSpec(
    name=name,
    repo=str(Path.cwd()),
    harness='claude',
    workspace_pinned=True,
    host=host,
    drop=drop,
    no_trails=no_trails,
    hold=hold,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
    llm=llm,
    resolved_llm=claude_harness.CLAUDE.resolve_llm(llm, resolved_bro).dump(),
    solo=solo,
    resume=resume,
    into=into,
    bro=resolved_bro,
    prompt=prompt,
    subject=prompt,
    arguments=arguments if arguments is not None else [],
    harness_options=ClaudeOptions(raw=raw).dump(),
  )


def _resume(
  name: str = 'w',
  *,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
) -> int:
  return ride_session.resume_session(
    name,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
  )


def _materialize_store(_store, directory: Path) -> Path:
  """The empty scoped store a host launch materializes."""
  directory.mkdir(parents=True, exist_ok=True)
  (directory / credentials.SOURCES_FILE).write_text('{}')
  return directory


def _scoped_store() -> dict[str, bytes]:
  """a hydrated scope with no secret in it — the registry file `build_scoped_store`
  always emits, and nothing else."""
  return {credentials.SOURCES_FILE: b'{}'}


def _launch_scope(**overrides) -> ride_session.ScopedLaunch:
  base = {
    'scoped': ScopedSecrets({'github'}, set()),
    'may_summon': set(),
    'store': _scoped_store(),
  }
  base.update(overrides)
  return ride_session.ScopedLaunch(**base)


def _workspace(tmp_path, kind: WorkspaceKind = WorkspaceKind.CONTAINER) -> Workspace:
  return Workspace.ensure('w', tmp_path, kind)


def _runtime_bundle(tmp_path) -> RuntimeBundle:
  root = tmp_path / 'runtime-bundle'
  (root / 'host' / 'venv' / 'bin').mkdir(parents=True, exist_ok=True)
  (root / 'host' / 'bin').mkdir(exist_ok=True)
  (root / 'host' / '.complete').touch()
  ride = root / 'host' / 'venv' / 'bin' / 'ride'
  ride.touch()
  ride.chmod(0o755)
  for command in ('broker', 'summon'):
    binary = shutil.which(command)
    assert binary is not None
    destination = root / 'host' / 'bin' / command
    if not destination.exists():
      destination.symlink_to(binary)
  return RuntimeBundle(root, '3.12')


@pytest.fixture(autouse=True)
def configured_project(monkeypatch, tmp_path):
  monkeypatch.chdir(tmp_path)
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  # a launch reads the human it credits out of git; without this the operator's
  # own configured identity would decide what these launches carry
  monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'gitconfig'))
  monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'gitconfig-system'))

  @contextlib.contextmanager
  def resolved_runtime_bundle():
    yield _runtime_bundle(tmp_path)

  monkeypatch.setattr(ride_session, 'resolve_runtime_bundle', resolved_runtime_bundle)
  # the suite itself may run inside a container; without this every launch would
  # hit the nested-launch refusal
  monkeypatch.setattr(ride_session, 'in_container', lambda: False)


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
      patch('ride.session.find_container_id', return_value=None),
      patch('ride.session.run_in_container', return_value=0),
      patch(
        'ride.session.scoped_secrets',
        return_value=ScopedSecrets(set(self.secrets), set(self.optional_secrets)),
      ),
      patch('ride.claude.harness.credentials.try_get', return_value='tok'),
      patch('ride.scope.credentials.build_scoped_store', return_value=({}, frozenset())),
      patch('ride.claude.harness.container_claude_state', return_value=([], {})),
      patch('ride.workspace.model.ContainerWorkspace.remove'),
      patch('ride.session._print_resume_hint'),
      # keep the bro-registry import out; threading is asserted per-test
      patch('ride.summon_control.summon_allow_list', return_value=set()),
      patch('ride.claude.harness.load_anthropic_key', return_value={'api_key': 'k'}),
      patch('ride.session.local_trails_mounts', return_value=()),
      patch(
        'ride.workspace.docker.ContainerRuntimeResolver.resolve',
        return_value=ContainerRuntime('runtime-image', 'bundle-hash'),
      ),
    ]
    entered = [p.__enter__() for p in self._patches]
    self.env = entered[0]
    self.env.pop('RIDE_BRO', None)
    self.run_in_container = entered[2]
    self.try_get = entered[4]
    self.build_scoped_store = entered[5]
    self.container_claude_state = entered[6]
    self.remove_workspace = entered[7]
    self.scoped_secrets = entered[3]
    self.summon_allow_list = entered[9]
    self.local_trails_mounts = entered[11]
    return self

  def __exit__(self, *exception):
    for p in reversed(self._patches):
      p.__exit__(*exception)
    return False


class TestRuntimeBundle:
  def test_resolution_failure_precedes_workspace_creation(self, monkeypatch, tmp_path, caplog):
    @contextlib.contextmanager
    def fail_resolution():
      raise RuntimeBundleError('unclassifiable installation')
      yield

    monkeypatch.setattr(ride_session, 'resolve_runtime_bundle', fail_resolution)

    assert ride_session.start_session(_spec(name='fresh')) == 1
    assert 'unclassifiable installation' in caplog.text
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()


class TestNestedLaunch:
  def test_ride_refuses_with_process_and_summon_remedies(self, monkeypatch, caplog):
    monkeypatch.setattr(ride_session, 'in_container', lambda: True)
    spec = replace(_spec(), workspace_pinned=False)
    assert ride_session.start_session(spec) == 1
    assert '`summon`' in caplog.text
    assert '`bro run|chat`' in caplog.text

  def test_an_unmarked_container_is_refused_too(self, monkeypatch, caplog):
    monkeypatch.delenv('RIDE_IN_CONTAINER', raising=False)
    monkeypatch.setattr(ride_session, 'in_container', lambda: True)
    assert ride_session.start_session(_spec()) == 1
    assert 'cannot start inside a container' in caplog.text


class TestGrantRevoke:
  def test_start_session_applies_grant_and_revoke(self):
    with _ContainerHarness(secrets={'notion', 'trails', 'github'}) as h:
      rc = ride_session.start_session(_spec(drop=True, grant=['gmail_creds'], revoke=['notion']))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'gmail_creds' in launch.secrets
    assert 'notion' not in launch.secrets

  def test_start_session_grant_replaces_a_credential_instance(self):
    with _ContainerHarness(secrets={'brog', 'github'}) as harness:
      rc = ride_session.start_session(_spec(drop=True, grant=['brog+github']))
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.secrets == {'brog', 'github'}
    assert launch.credential_selection['brog'] == 'github'

  def test_start_session_can_revoke_an_optional_secret(self):
    with _ContainerHarness(optional_secrets={'openai'}) as harness:
      rc = ride_session.start_session(_spec(drop=True, revoke=['openai']))
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.optional_secrets == set()

  def test_missing_secret_fails_cleanly_before_container_launch(self, caplog):
    with _ContainerHarness() as harness:
      harness.build_scoped_store.side_effect = credentials.SecretNotFound('github')
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 1
    assert harness.run_in_container.call_count == 0
    assert 'github' in caplog.text

  def test_missing_setup_token_has_actionable_container_error(self, caplog):
    with _ContainerHarness() as harness:
      harness.try_get.return_value = None
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 1
    assert harness.run_in_container.call_count == 0
    assert 'mint one with `claude setup-token`' in caplog.text

  def test_missing_setup_token_does_not_gate_a_raw_launch(self):
    with _ContainerHarness() as harness:
      harness.try_get.return_value = None
      rc = ride_session.start_session(_spec(drop=True, raw=True))
    assert rc == 0

  def test_start_session_grant_already_present_returns_1(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, grant=['github']))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_start_session_injects_the_llm_recipe_into_the_container_command(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, llm='::xhigh'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command[command.index('--llm') + 1] == '::xhigh'


class TestNoTrails:
  """the neutral --no-trails handling: the trails scope baseline, env kill
  switch, and trails mounts are the session layer's, whichever harness runs."""

  def test_no_trails_strips_the_scope_baseline(self):
    with _ContainerHarness() as h:
      assert ride_session.start_session(_spec(drop=True, no_trails=True)) == 0
    assert h.scoped_secrets.call_args.args[1].optional_baseline == frozenset()

  def test_no_trails_disables_recording_and_binds_no_trails_root(self):
    with _ContainerHarness() as h:
      assert ride_session.start_session(_spec(drop=True, no_trails=True)) == 0
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['TRAILS_DISABLED'] == '1'
    assert h.local_trails_mounts.call_count == 0

  def test_recording_stays_on_by_default(self):
    with _ContainerHarness() as h:
      assert ride_session.start_session(_spec(drop=True)) == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'TRAILS_DISABLED' not in launch.env
    assert h.scoped_secrets.call_args.args[1].optional_baseline == frozenset({'trails'})


class TestSummonAllowList:
  def test_container_session_threads_the_allow_list(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.return_value = {'dev'}
      rc = ride_session.start_session(_spec(drop=True, grant=['@dev']))
    assert rc == 0
    assert h.summon_allow_list.call_args == (
      ('bro-dev',),
      {'grant': ['dev'], 'revoke': []},
    )
    assert h.run_in_container.call_args.kwargs['may_summon'] == {'dev'}

  def test_container_session_keys_identity_on_the_bro(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev'))
    assert rc == 0
    assert h.summon_allow_list.call_args[0] == ('dev',)

  def test_bad_summon_flag_fails_the_launch(self):
    with _ContainerHarness() as h:
      h.summon_allow_list.side_effect = ValueError('unknown summon target(s): devoop')
      rc = ride_session.start_session(_spec(drop=True, grant=['@devoop']))
    assert rc == 1
    assert h.run_in_container.call_count == 0


def _configure_human(tmp_path, name: str = 'Ada Lovelace', email: str = 'ada@example.com'):
  """the launching human's own git configuration, as the host carries it."""
  (tmp_path / 'gitconfig').write_text(f'[user]\n\tname = {name}\n\temail = {email}\n')


class TestHumanIdentity:
  """the human a session credits travels with its launch: a container carries
  none of the host's git configuration to read it back from."""

  def test_container_launch_carries_the_attachments_human(self, tmp_path):
    _configure_human(tmp_path)
    with _ContainerHarness() as harness:
      assert ride_session.start_session(_spec(drop=True)) == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.env[HUMAN_NAME_ENV] == 'Ada Lovelace'
    assert launch.env[HUMAN_EMAIL_ENV] == 'ada@example.com'

  def test_a_detached_launch_carries_no_human(self, tmp_path):
    _configure_human(tmp_path)
    with _ContainerHarness() as harness:
      assert ride_session.start_session(replace(_spec(drop=True), repo=None)) == 0
    launch = harness.run_in_container.call_args.args[0]
    assert HUMAN_NAME_ENV not in launch.env
    assert HUMAN_EMAIL_ENV not in launch.env


class TestDetachedSession:
  def test_container_launch_records_no_attachment_or_branch(self):
    spec = replace(_spec(drop=False), repo=None)
    with _ContainerHarness() as harness:
      assert ride_session.start_session(spec) == 0
      assert 'RIDE_REPO' not in os.environ
      assert '--repo' not in os.environ['RIDE_COMMAND']
    workspace = Workspace.open('w')
    assert workspace.repo is None
    assert workspace.metadata.branch is None
    launch = harness.run_in_container.call_args.args[0]
    assert '--repo' not in launch.command
    assert 'RIDE_BASE_REF' not in launch.env

  def test_into_is_refused_without_an_attachment(self, caplog):
    spec = replace(_spec(into='feature'), repo=None)
    with _ContainerHarness() as harness:
      assert ride_session.start_session(spec) == 1
    assert '--into requires --repo' in caplog.text
    harness.run_in_container.assert_not_called()

  def test_host_launch_uses_a_plain_directory_and_skips_project_setup(self, tmp_path):
    spec = replace(_spec(host=True), repo=None)
    workspace = Workspace.create('detached-host', None, WorkspaceKind.WORKTREE)
    harness = MagicMock()
    harness.inner_flags.return_value = []
    with (
      patch('ride.session.get_harness', return_value=harness),
      patch('ride.session.ensure_host_worktree') as ensure_worktree,
      patch('ride.session.provision_host_worktree') as provision_worktree,
      patch('ride.session.materialize_scoped_store', new=_materialize_store),
      patch('ride.session.broker_enabled', return_value=False),
      patch('ride.session.subprocess.run', return_value=MagicMock(returncode=0)),
    ):
      code = ride_session._host_session(
        harness,
        spec,
        workspace,
        None,
        _launch_scope(),
        {},
        _runtime_bundle(tmp_path),
        ContainerRuntimeResolver.fixed(ContainerRuntime('runtime', 'hash')),
      )
    assert code == 0
    assert workspace.tree.is_dir()
    ensure_worktree.assert_not_called()
    provision_worktree.assert_not_called()


class TestContainerCommand:
  def test_command_is_the_in_place_invocation(self):
    # the docker command is the same in-place runner host mode spawns; the
    # argv/MCP/spell-delivery work happens inside the container, next to claude
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev', llm='::xhigh+fast', prompt='go'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride', 'along', '--in-place', '--workspace', 'w', '--harness', 'claude', '--repo', str(Path.cwd()),
      '--hold', 'attended', '--llm', '::xhigh+fast', 'dev', 'go',
    ]  # fmt: skip

  def test_bro_carried_in_command_and_stamped_into_the_container_env(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev'))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'attended',
      'dev',
    ]
    # RIDE_BRO themes the whole container (ride exec shells), set explicitly in the
    # container env — never forwarded from the launcher's environment
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BRO'] == 'dev'

  def test_local_trails_data_is_combined_with_claude_launch_data(self):
    with _ContainerHarness(secrets={'github', 'trails'}) as harness:
      harness.container_claude_state.return_value = (
        ['/host/claude:/home/ride/.claude'],
        {'CLAUDE_CONFIG_DIR': '/home/ride/.claude'},
      )
      harness.local_trails_mounts.return_value = ('/host/trails:/var/ride/trails',)
      result = ride_session.start_session(_spec(drop=True))

    assert result == 0
    harness.local_trails_mounts.assert_called_once_with(ScopedSecrets({'github', 'trails'}, set()))
    launch, workspace = harness.run_in_container.call_args.args[:2]
    assert launch.extra_mounts == (
      '/host/claude:/home/ride/.claude',
      '/host/trails:/var/ride/trails',
      f'{workspace_session_dir(workspace.path)}:{CONTAINER_SESSION_DIR}',
    )

  def test_raw_carried_in_the_container_command(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True, bro='dev', raw=True))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      '--raw',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'attended',
      'dev',
    ]

  def test_solo_launch_has_no_container_tty(self):
    spec = replace(
      _spec(solo=True, hold='unattended', prompt='go'),
      workspace_pinned=False,
    )
    with _ContainerHarness() as harness:
      assert ride_session.start_session(spec) == 0
    launch = harness.run_in_container.call_args.args[0]
    assert not launch.tty
    assert launch.command == [
      'ride', 'solo', '--in-place', '--workspace', 'w', '--harness', 'claude', '--repo', str(Path.cwd()),
      '--hold', 'unattended', 'bro-dev', 'go',
    ]  # fmt: skip

  def test_ride_session_stamps_the_default_bro_as_ride_bro(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BRO'] == 'bro-dev'

  def test_default_base_is_left_to_the_entrypoint_head_fallback(self):
    # no RIDE_BASE_REF by default: the clone bases on HEAD — the host checkout as
    # cloned — with no network touched on the way
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 0
    launch = h.run_in_container.call_args.args[0]
    assert 'RIDE_BASE_REF' not in launch.env

  def test_url_attachment_uses_fresh_origin_head_as_the_default_base(self, tmp_path):
    mirror = tmp_path / 'mirror'
    mirror.mkdir()
    repository = Repository('https://example.test/owner/repo.git', mirror, 'urlsha')
    spec = replace(_spec(drop=True), repo=repository.identity)
    with _ContainerHarness() as harness:
      with (
        patch('ride.session.hold_repository', return_value=contextlib.nullcontext(repository)),
        patch('ride.workspace.model.open_repository', return_value=repository),
      ):
        rc = ride_session.start_session(spec, repository)
    assert rc == 0
    launch = harness.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BASE_REF'] == 'urlsha'
    assert launch.repo == repository
    assert Workspace.open('w').metadata.repo == repository.identity

  def test_into_threads_the_resolved_base_into_the_container_env(self):
    with _ContainerHarness() as h:
      with patch('ride.session.resolve_ref', return_value='intosha') as resolve:
        rc = ride_session.start_session(_spec(drop=True, into='feature'))
    assert rc == 0
    assert resolve.call_args[0][1] == 'feature'
    launch = h.run_in_container.call_args.args[0]
    assert launch.env['RIDE_BASE_REF'] == 'intosha'

  def test_unresolvable_into_fails_launch(self):
    with _ContainerHarness() as h:
      with patch('ride.session.resolve_ref', return_value=None):
        rc = ride_session.start_session(_spec(drop=True, into='nope'))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_guard_fails_fast_without_a_session(self, tmp_path):
    with _ContainerHarness() as h:
      with patch('ride.claude.harness.workspace_projects_dir') as projects:
        projects.return_value = tmp_path / 'projects'
        rc = ride_session.start_session(_spec(resume=True))
    assert rc == 1
    assert h.run_in_container.call_count == 0

  def test_resume_carried_as_bare_flag_the_runner_resolves(self, tmp_path):
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    (projects_dir / 'abc.jsonl').write_text('{}')
    with _ContainerHarness() as h:
      with patch('ride.claude.harness.workspace_projects_dir') as projects:
        projects.return_value = projects_dir
        rc = ride_session.start_session(_spec(resume=True))
    assert rc == 0
    command = h.run_in_container.call_args.args[0].command
    assert command == [
      'ride',
      'along',
      '--in-place',
      '--workspace',
      'w',
      '--harness',
      'claude',
      '--resume',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'attended',
      'bro-dev',
    ]


class TestContainerDrop:
  def test_drop_removes_the_workspace_on_clean_exit(self):
    with _ContainerHarness() as h:
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 0
    assert h.remove_workspace.call_count == 1

  def test_drop_keeps_the_workspace_when_the_session_failed(self):
    with _ContainerHarness() as h:
      h.run_in_container.return_value = 3
      rc = ride_session.start_session(_spec(drop=True))
    assert rc == 3
    assert h.remove_workspace.call_count == 0


class TestCommandArgv:
  def test_create_command_includes_drop_into_and_forwarded_arguments(self):
    parts = _spec(
      hold='attended',
      drop=True,
      llm='::xhigh+fast',
      bro='dev',
      grant=['gmail_creds', '@bro'],
      revoke=['notion'],
      into='feature',
      arguments=['--foo'],
    ).to_command_argv()
    assert parts == [
      'ride', 'along', '--drop', '--repo', str(Path.cwd()), '--hold', 'attended', '--llm', '::xhigh+fast',
      '--harness', 'claude', '--workspace', 'w', '--grant', 'gmail_creds',
      '--grant', '@bro', '--revoke', 'notion', '--into', 'feature', 'dev',
      '--', '--foo',
    ]  # fmt: skip

  def test_host_session_carries_the_host_flag(self):
    parts = _spec(host=True, hold='detached').to_command_argv()
    assert parts == [
      'ride',
      'along',
      '--host',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'detached',
      '--harness',
      'claude',
      '--workspace',
      'w',
      'bro-dev',
    ]

  def test_the_resolved_hold_is_restated_even_at_its_default(self):
    # the reconstruction never trusts a re-parse to re-derive the default
    assert _spec(hold='attended').to_command_argv() == [
      'ride',
      'along',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'attended',
      '--harness',
      'claude',
      '--workspace',
      'w',
      'bro-dev',
    ]

  def test_solo_command_uses_keep_only_for_a_retained_automatic_workspace(self):
    automatic = replace(
      _spec(solo=True, drop=True, hold='unattended', prompt='go'),
      workspace_pinned=False,
    )
    assert automatic.to_command_argv() == [
      'ride',
      'solo',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'unattended',
      '--harness',
      'claude',
      'bro-dev',
      'go',
    ]
    assert replace(automatic, drop=False).to_command_argv() == [
      'ride',
      'solo',
      '--keep',
      '--repo',
      str(Path.cwd()),
      '--hold',
      'unattended',
      '--harness',
      'claude',
      'bro-dev',
      'go',
    ]

  def test_a_resume_is_its_own_command(self):
    # the recorded spec carries the flags, so the name is the whole command
    assert _spec(hold='attended', bro='dev').resume_variant().to_command_argv() == [
      'ride',
      'resume',
      'w',
    ]


class TestScopeOverrides:
  def test_values_join_the_recorded_lists(self):
    updated = _spec(grant=['brog+github'], revoke=['openai']).with_scope_overrides(
      grant=['@bro-dev'], revoke=['brave']
    )
    assert updated.grant == ['brog+github', '@bro-dev']
    assert updated.revoke == ['openai', 'brave']

  def test_an_override_cancels_the_opposite_recorded_one(self):
    # granting back a revoked credential leaves the computed scope's own selection
    updated = _spec(grant=['@bro-dev'], revoke=['openai']).with_scope_overrides(
      grant=['openai'], revoke=['@bro-dev']
    )
    assert (updated.grant, updated.revoke) == ([], [])

  def test_revoke_kind_cancels_a_recorded_instance_grant(self):
    updated = _spec(grant=['github+reviewer']).with_scope_overrides(grant=[], revoke=['github'])
    assert (updated.grant, updated.revoke) == ([], [])

  def test_a_new_instance_grant_replaces_the_recorded_same_kind_grant(self):
    updated = _spec(grant=['github+reviewer']).with_scope_overrides(
      grant=['github+developer'], revoke=[]
    )
    assert updated.grant == ['github+developer']

  def test_instance_spelled_revoke_is_rejected_on_resume(self):
    with pytest.raises(ValueError, match=r'revoke its kind instead \(--revoke github\)'):
      _spec(grant=['github+reviewer']).with_scope_overrides(grant=[], revoke=['github+reviewer'])

  def test_restating_a_recorded_override_raises(self):
    with pytest.raises(ValueError, match='already in the recorded --grant: @bro-dev'):
      _spec(grant=['@bro-dev']).with_scope_overrides(grant=['@bro-dev'], revoke=[])
    with pytest.raises(ValueError, match='already in the recorded --revoke: openai'):
      _spec(revoke=['openai']).with_scope_overrides(grant=[], revoke=['openai'])

  def test_a_contradicting_pair_survives_for_the_scope_layer(self):
    # nothing recorded to cancel, so both land and the launch preflight rejects them
    updated = _spec().with_scope_overrides(grant=['openai'], revoke=['openai'])
    assert (updated.grant, updated.revoke) == (['openai'], ['openai'])


class TestResumeSpecRecord:
  def test_recorded_spec_clears_create_only_inputs_and_round_trips(self, tmp_path):
    spec = _spec(
      hold='attended',
      drop=True,
      llm='::xhigh',
      bro='dev',
      grant=['gmail_creds'],
      into='feature',
      prompt='do it',
      arguments=['--foo'],
    )
    workspace = _workspace(tmp_path)
    ride_session.record_resume_spec(workspace, spec)
    loaded = ride_session.load_resume_spec(workspace)
    assert loaded == spec.resume_variant()
    assert loaded is not None and loaded.resume and not loaded.drop
    assert loaded.into is None and loaded.prompt is None and loaded.arguments == []
    # the forwarded flags survive, so the resumed session runs as it was launched
    assert (loaded.hold, loaded.llm, loaded.bro, loaded.grant) == (
      'attended',
      '::xhigh',
      'dev',
      ['gmail_creds'],
    )

  def test_solo_resume_becomes_an_along_session_with_its_default_hold(self, tmp_path):
    solo = replace(
      _spec(solo=True, hold='unattended', prompt='go'),
      workspace_pinned=False,
    )
    workspace = _workspace(tmp_path)
    ride_session.record_resume_spec(workspace, solo)
    loaded = ride_session.load_resume_spec(workspace)
    assert loaded is not None
    assert not loaded.solo
    assert loaded.hold == 'attended'
    assert loaded.to_command_argv() == ['ride', 'resume', 'w']

  def test_recording_a_resume_is_a_fixpoint(self, tmp_path):
    workspace = _workspace(tmp_path)
    ride_session.record_resume_spec(workspace, _spec(bro='dev'))
    first = ride_session.load_resume_spec(workspace)
    assert first is not None
    ride_session.record_resume_spec(workspace, first)
    assert ride_session.load_resume_spec(workspace) == first

  def test_missing_record_reads_as_none(self, tmp_path):
    assert ride_session.load_resume_spec(_workspace(tmp_path)) is None

  def test_record_from_an_incompatible_ride_reads_as_none(self, tmp_path, caplog):
    workspace = _workspace(tmp_path)
    workspace.resume_file.write_text(json.dumps({'name': 'w', 'gone': True}))
    assert ride_session.load_resume_spec(workspace) is None
    assert 'unreadable resume spec' in caplog.text

  def test_start_session_records_before_launching(self, tmp_path):
    with _ContainerHarness():
      recorded: list = []
      with patch(
        'ride.session._container_session',
        side_effect=lambda *args: recorded.append(ride_session.load_resume_spec(args[2])) or 0,
      ):
        assert ride_session.start_session(_spec(drop=True, bro='dev')) == 0
    assert recorded[0] == _spec(drop=True, bro='dev').resume_variant()


class TestResumeSession:
  def test_relaunches_the_recorded_spec(self, tmp_path):
    ride_session.record_resume_spec(_workspace(tmp_path), _spec(bro='dev', hold='attended'))
    with patch('ride.session.start_session', return_value=0) as start:
      assert _resume() == 0
    assert start.call_args[0][0] == _spec(bro='dev', hold='attended').resume_variant()

  def test_scope_overrides_reach_the_relaunch(self, tmp_path):
    ride_session.record_resume_spec(_workspace(tmp_path), _spec(grant=['brog+github']))
    with patch('ride.session.start_session', return_value=0) as start:
      assert _resume(grant=['@bro-dev']) == 0
    assert start.call_args[0][0].grant == ['brog+github', '@bro-dev']

  def test_a_no_op_override_errors(self, tmp_path, caplog):
    ride_session.record_resume_spec(_workspace(tmp_path), _spec(grant=['@bro-dev']))
    with patch('ride.session.start_session') as start:
      assert _resume(grant=['@bro-dev']) == 1
    assert start.call_count == 0
    assert 'already in the recorded --grant: @bro-dev' in caplog.text

  def test_unknown_workspace_errors(self, caplog):
    with patch('ride.session.start_session') as start:
      assert _resume('gone') == 1
    assert start.call_count == 0
    assert 'workspace not found: gone' in caplog.text

  def test_workspace_without_a_record_errors(self, tmp_path, caplog):
    _workspace(tmp_path)
    with patch('ride.session.start_session') as start:
      assert _resume() == 1
    assert start.call_count == 0
    assert 'no session recorded for w' in caplog.text


class TestResumeHint:
  HINT = 'Resume this session with:\n  ride resume w\n'

  def test_prints_the_resume_command_and_nothing_else(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(claude_harness.CLAUDE, 'session_exists', lambda workspace: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    ride_session._print_resume_hint(_spec(solo=True, prompt='go'), _workspace(tmp_path))
    assert capsys.readouterr().out == self.HINT

  def test_silent_without_a_session_jsonl(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(claude_harness.CLAUDE, 'session_exists', lambda workspace: False)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    ride_session._print_resume_hint(_spec(host=True), _workspace(tmp_path, WorkspaceKind.WORKTREE))
    assert capsys.readouterr().out == ''

  def test_silent_when_stdout_is_redirected(self, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(claude_harness.CLAUDE, 'session_exists', lambda workspace: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: False)
    ride_session._print_resume_hint(_spec(), _workspace(tmp_path))
    assert capsys.readouterr().out == ''


class TestHarnessScopeRecipe:
  def test_raw_selects_the_harness_scope_recipe(self):
    assert claude_harness.CLAUDE.scope_recipe(_spec(raw=True).harness_options).name == 'claude-raw'
    assert claude_harness.CLAUDE.scope_recipe(_spec().harness_options).name == 'claude-full'


class TestConcurrentSessionGuard:
  """the one-session-per-workspace lock, taken by start_session before either mode
  prepares anything."""

  @pytest.fixture(autouse=True)
  def launch_preflights(self, monkeypatch):
    # start_session runs the auth and scope preflights ahead of the guards these
    # tests drive; without stubs they read the machine's own credential store
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set())
    )
    monkeypatch.setattr(
      ride.scope.credentials,
      'build_scoped_store',
      lambda store, names, optional=(): (_scoped_store(), frozenset()),
    )
    monkeypatch.setattr(ride.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    # the shared active-container refusal probes docker ahead of the launch body
    monkeypatch.setattr(ride_session, 'find_container_id', lambda tree: None)

  def test_second_launch_is_refused_while_the_lock_is_held(self, tmp_path, caplog):
    workspace = _workspace(tmp_path)
    with workspace.hold_session_lock():
      with patch('ride.session._container_session') as launch:
        assert ride_session.start_session(_spec()) == 1
    assert launch.call_count == 0
    assert 'session already active on workspace' in caplog.text

  def test_the_lock_releases_with_the_session(self, tmp_path):
    with patch('ride.session._container_session', return_value=0):
      assert ride_session.start_session(_spec()) == 0
    assert not _workspace(tmp_path).is_active(set())

  def test_a_launch_naming_a_workspace_of_the_other_kind_is_refused(self, tmp_path, caplog):
    _workspace(tmp_path, WorkspaceKind.WORKTREE)
    with patch('ride.session._container_session') as launch:
      assert ride_session.start_session(_spec()) == 1
    assert launch.call_count == 0
    assert 'is a worktree workspace, not container' in caplog.text

  def test_container_refuses_an_orphaned_running_container(self, monkeypatch, tmp_path, caplog):
    # a launcher killed outright releases the lock but leaves its container bound
    monkeypatch.setattr(ride_session, 'find_container_id', lambda path: 'abc123')

    def boom(*_a, **_k):
      raise AssertionError('must not launch a second container session')

    monkeypatch.setattr(ride_session, 'run_in_container', boom)
    assert ride_session.start_session(_spec()) == 1
    assert 'session already active in the container' in caplog.text


class TestHostSession:
  def _fake_workspace(self, monkeypatch, tmp_path, *, has_session: bool):
    projects = tmp_path / 'projects'
    projects.mkdir()
    if has_session:
      (projects / 'abc.jsonl').write_text('{}')
    workspace = _workspace(tmp_path, WorkspaceKind.WORKTREE)
    monkeypatch.setattr(claude_harness, 'workspace_projects_dir', lambda ws: projects)
    monkeypatch.setattr(type(workspace), 'remove', lambda self: None)
    return workspace, workspace.tree

  def _host_session(self, spec, workspace, launch_scope, human_env=None):
    return ride_session._launch_session(
      spec,
      workspace,
      None,
      launch_scope,
      human_env={} if human_env is None else human_env,
      container=False,
      runtime_bundle=_runtime_bundle(workspace.repo or workspace.path),
      container_runtime=ContainerRuntimeResolver.fixed(
        ContainerRuntime('runtime-image', 'bundle-hash')
      ),
    )

  def _prepare_launch(self, monkeypatch, tmp_path):
    workspace, worktree = self._fake_workspace(monkeypatch, tmp_path, has_session=False)
    ride_binary = _runtime_bundle(tmp_path).host_venv / 'bin' / 'ride'
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(ride_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(ride_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(ride_session, 'provision_host_worktree', lambda *_a: True)
    # keep the launch tests off the real credential store; the auth-transform
    # test overrides this with its own fake
    monkeypatch.setattr(claude_harness, 'apply_claude_auth', lambda env, **_k: None)
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      claude_harness,
      'provision_host_claude_dir',
      lambda ws, wt, project: tmp_path / 'claude-config',
    )
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets({'github'}, set())
    )
    monkeypatch.setattr(
      ride.scope.credentials,
      'build_scoped_store',
      lambda store, names, optional=(): (_scoped_store(), frozenset()),
    )
    monkeypatch.setattr(ride_session, 'materialize_scoped_store', _materialize_store)
    monkeypatch.setattr(ride.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    return workspace, ride_binary, worktree

  def test_broker_supervises_the_snapshot_in_place_runner(self, monkeypatch, tmp_path):
    workspace, ride_binary, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: True)
    monkeypatch.setattr(ride.summon_control, 'summon_allow_list', lambda *_a, **_k: {'dev'})
    roots: list = []

    def fake_root(
      root_workspace,
      command,
      env,
      may_summon,
      credential_scope,
      container_runtime,
      *,
      interactive,
    ):
      roots.append(
        {
          'workspace': root_workspace,
          'command': command,
          'env': env,
          'may_summon': may_summon,
          'credential_scope': credential_scope,
          'container_runtime': container_runtime,
          'interactive': interactive,
        }
      )
      return 5

    monkeypatch.setattr(ride_session, 'run_host_process_via_broker', fake_root)
    spec = _spec(host=True, hold='attended', llm='::xhigh', prompt='go', arguments=['--foo'])
    scope = _launch_scope(may_summon={'dev'})
    assert self._host_session(spec, workspace, scope) == 5
    assert roots[0]['workspace'] is workspace
    assert roots[0]['command'] == [
      str(ride_binary), 'along', '--in-place', '--workspace', 'w', '--harness', 'claude', '--repo', str(Path.cwd()),
      '--hold', 'attended', '--llm', '::xhigh', 'bro-dev', 'go', '--', '--foo',
    ]  # fmt: skip
    assert 'VIRTUAL_ENV' not in roots[0]['env']
    assert str(worktree / '.venv' / 'bin') not in roots[0]['env']['PATH'].split(os.pathsep)
    # the host root gets the session's summon allow-list like container mode
    assert roots[0]['may_summon'] == {'dev'}
    assert roots[0]['interactive']

  def test_host_runner_env_carries_the_summon_facts(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_run_root(launch, **_kwargs):
      captured['launch'] = launch
      captured['env'] = launch.env
      return 0

    monkeypatch.setattr(ride.spawn, 'run_root_via_broker', fake_run_root)
    assert (
      ride_session.run_host_process_via_broker(
        workspace,
        ['ride'],
        {},
        {'dev', 'bro'},
        ScopedSecrets(set(), set()),
        ContainerRuntimeResolver.fixed(ContainerRuntime('runtime-image', 'bundle-hash')),
        interactive=False,
      )
      == 0
    )
    assert captured['env'][bro.summon.MAY_SUMMON_ENV] == 'bro,dev'
    assert captured['env'][ride.summon_control.STATUS_ENV].endswith('w.status.json')
    assert not captured['launch'].interactive

  def test_bad_summon_flag_fails_before_the_workspace_is_recorded(self, monkeypatch, tmp_path):
    self._prepare_launch(monkeypatch, tmp_path)

    def bad_allow_list(*_a, **_k):
      raise ValueError('unknown summon target(s): devoop')

    monkeypatch.setattr(ride.summon_control, 'summon_allow_list', bad_allow_list)

    def boom(*_a, **_k):
      raise AssertionError('must not launch when the summon grant is bad')

    monkeypatch.setattr(ride_session, '_host_session', boom)
    assert ride_session.start_session(_spec(name='fresh', host=True, grant=['@devoop'])) == 1
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()

  def test_direct_spawn_when_broker_disabled(self, monkeypatch, tmp_path):
    workspace, ride_binary, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ride_session.subprocess, 'run', fake_run)
    spec = _spec(host=True, hold='attended', llm='::xhigh', prompt='go', arguments=['--foo'])
    assert self._host_session(spec, workspace, _launch_scope()) == 0
    argv, kwargs = runs[0]
    assert argv == [
      str(ride_binary), 'along', '--in-place', '--workspace', 'w', '--harness', 'claude', '--repo', str(Path.cwd()),
      '--hold', 'attended', '--llm', '::xhigh', 'bro-dev', 'go', '--', '--foo',
    ]  # fmt: skip
    assert kwargs['cwd'] == str(worktree)
    assert 'VIRTUAL_ENV' not in kwargs['env']

  def test_summoned_host_run_attaches_to_the_summoners_socket_and_claims(
    self, monkeypatch, tmp_path
  ):
    # a summoned host session runs the direct spawn — no broker of its own — with
    # the session broxy kept, pointed at the summoner's socket
    workspace, _, worktree = self._prepare_launch(monkeypatch, tmp_path)
    record = _pending_record(tmp_path)
    monkeypatch.setattr(
      ride_session,
      'run_host_process_via_broker',
      lambda *_a, **_k: pytest.fail('a summoned session must not start its own broker'),
    )
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ride_session.subprocess, 'run', fake_run)
    spec = _spec(host=True, prompt='pair on this')
    assert (
      ride_session._launch_session(
        spec,
        workspace,
        None,
        _launch_scope(),
        human_env={},
        container=False,
        runtime_bundle=_runtime_bundle(workspace.repo or workspace.path),
        container_runtime=ContainerRuntimeResolver.fixed(
          ContainerRuntime('runtime-image', 'bundle-hash')
        ),
        summoned=record,
      )
      == 0
    )
    _, kwargs = runs[0]
    env = kwargs['env']
    assert env['BROKER_CHANNEL'] == record.address()
    assert env['RIDE_SUMMONED'] == '1'
    assert env['RIDE_MAY_SUMMON'] == 'dev'
    assert env['RIDE_WORKSPACE'] == 'w'
    assert env[ride_session.START_SESSION_BROXY_ENV] == '1'
    assert kwargs['cwd'] == str(worktree)
    with pytest.raises(pending_summon.UnknownToken):
      pending_summon.peek(record.token)

  def test_summoned_host_run_fails_cleanly_on_a_spent_token(self, monkeypatch, tmp_path, caplog):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    record = _pending_record(tmp_path)
    pending_summon.claim(record.token, workspace='spender')
    monkeypatch.setattr(
      ride_session.subprocess,
      'run',
      lambda *_a, **_k: pytest.fail('a spent token must not start a session'),
    )
    assert (
      ride_session._launch_session(
        _spec(host=True),
        workspace,
        None,
        _launch_scope(),
        human_env={},
        container=False,
        runtime_bundle=_runtime_bundle(workspace.repo or workspace.path),
        container_runtime=ContainerRuntimeResolver.fixed(
          ContainerRuntime('runtime-image', 'bundle-hash')
        ),
        summoned=record,
      )
      == 1
    )
    assert 'no pending manual summon' in caplog.text

  def test_runner_env_gets_the_claude_auth_transform(self, monkeypatch, tmp_path):
    # the outer applies auth to the runner env before the snapshot inner starts
    workspace, ride_binary, worktree = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)

    def fake_apply(env, **_kwargs):
      env['CLAUDE_CODE_OAUTH_TOKEN'] = 'applied'

    monkeypatch.setattr(claude_harness, 'apply_claude_auth', fake_apply)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ride_session.subprocess, 'run', fake_run)
    assert self._host_session(_spec(host=True), workspace, _launch_scope()) == 0
    assert runs[0][1]['env']['CLAUDE_CODE_OAUTH_TOKEN'] == 'applied'

  def test_runner_env_points_at_the_private_claude_config_dir(self, monkeypatch, tmp_path):
    # the outer provisions the per-session state before the snapshot inner starts
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      from types import SimpleNamespace

      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ride_session.subprocess, 'run', fake_run)
    assert self._host_session(_spec(host=True), workspace, _launch_scope()) == 0
    assert runs[0][1]['env']['CLAUDE_CONFIG_DIR'] == str(tmp_path / 'claude-config')

  def test_host_runner_env_carries_the_human_the_session_credits(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: True)
    root = MagicMock(return_value=0)
    monkeypatch.setattr(ride_session, 'run_host_process_via_broker', root)
    human = {HUMAN_NAME_ENV: 'Ada Lovelace', HUMAN_EMAIL_ENV: 'ada@example.com'}
    assert self._host_session(_spec(host=True), workspace, _launch_scope(), human) == 0
    assert root.call_args.args[2][HUMAN_NAME_ENV] == 'Ada Lovelace'
    assert root.call_args.args[2][HUMAN_EMAIL_ENV] == 'ada@example.com'

  def test_host_runner_env_signals_the_session_broxy(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: True)
    root = MagicMock(return_value=0)
    monkeypatch.setattr(ride_session, 'run_host_process_via_broker', root)
    assert self._host_session(_spec(host=True), workspace, _launch_scope()) == 0
    assert root.call_args.args[2][ride_session.START_SESSION_BROXY_ENV] == '1'

  def test_brokerless_spawn_unsets_an_ambient_channel(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setenv('BROKER_CHANNEL', 'tcp://ambient-token@127.0.0.1:9')
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ride_session.subprocess, 'run', fake_run)
    assert self._host_session(_spec(host=True), workspace, _launch_scope()) == 0
    assert 'BROKER_CHANNEL' not in runs[0][1]['env']
    assert ride_session.START_SESSION_BROXY_ENV not in runs[0][1]['env']

  def test_missing_claude_code_fails_a_ride_session_launch_before_the_workspace(
    self, monkeypatch, tmp_path
  ):
    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(credentials, 'try_get', lambda name: None)

    def boom(*_a, **_k):
      raise AssertionError('must not launch without the setup-token')

    monkeypatch.setattr(ride_session, '_host_session', boom)
    assert ride_session.start_session(_spec(name='fresh', host=True)) == 1
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()

  def test_runner_env_points_at_the_scoped_store_registry(self, monkeypatch, tmp_path):
    from types import SimpleNamespace

    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)
    materialized: dict = {}

    def fake_materialize(store, directory):
      materialized.update(store=store, directory=directory)
      return _materialize_store(store, directory)

    monkeypatch.setattr(ride_session, 'materialize_scoped_store', fake_materialize)
    runs: list = []

    def fake_run(argv, **kwargs):
      runs.append((argv, kwargs))
      return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ride_session.subprocess, 'run', fake_run)
    scope = _launch_scope(store={'creds/x.cred': b'v'})
    assert self._host_session(_spec(host=True), workspace, scope) == 0
    store_directory = workspace.path / 'credentials'
    assert runs[0][1]['env']['BRO_STORE'] == str(store_directory)
    assert materialized['store'] == {'creds/x.cred': b'v'}
    assert materialized['directory'] == store_directory

  def test_runner_env_carries_the_installed_credential_wiring(self, monkeypatch, tmp_path):
    workspace, _, _ = self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: True)
    monkeypatch.setattr(ride_session, 'materialize_scoped_store', materialize_scoped_store)
    root = MagicMock(return_value=0)
    monkeypatch.setattr(ride_session, 'run_host_process_via_broker', root)
    hook = {'files': {'file': 'wired'}, 'env': {'WIRED': 'yes'}}
    registry = {'x': credentials.CredentialKind('x', 'test credential', install=hook)}
    monkeypatch.setattr(credentials, 'default_registry', lambda: registry)
    store = {'creds.json': b'{}', 'creds/x.cred': b'v'}

    assert (
      self._host_session(
        _spec(host=True),
        workspace,
        _launch_scope(store=store, hydrated_kinds=frozenset({'x'})),
      )
      == 0
    )
    assert root.call_args.args[2]['WIRED'] == 'yes'
    assert (workspace.path / 'environment' / 'file').read_text() == 'wired'

  def test_grant_and_revoke_shape_and_log_the_hydrated_scope(self, monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace

    self._prepare_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)
    monkeypatch.setattr(
      ride_session,
      'scoped_secrets',
      lambda *_a, **_k: ScopedSecrets({'github', 'notion'}, {'openai'}),
    )
    hydrated: dict = {}

    def fake_build(store, names, optional=()):
      hydrated.update(names=set(names), optional=set(optional))
      return {}, frozenset()

    monkeypatch.setattr(ride.scope.credentials, 'build_scoped_store', fake_build)
    # a stand-in for every process the launch runs, the git the human identity
    # is read with among them
    monkeypatch.setattr(
      ride_session.subprocess,
      'run',
      lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='', stderr=''),
    )
    spec = _spec(host=True, grant=['gmail_creds'], revoke=['notion'])
    with caplog.at_level('INFO'):
      assert ride_session.start_session(spec) == 0
    assert hydrated == {
      'names': {'github', 'gmail_creds'},
      'optional': {'openai'},
    }
    assert 'scoped secrets for w: github, gmail_creds' in caplog.text
    assert 'optional (best-effort) secrets for w: openai' in caplog.text

  def test_unresolvable_secret_fails_before_the_workspace(self, monkeypatch, tmp_path):
    self._prepare_launch(monkeypatch, tmp_path)

    def missing(store, names, optional=()):
      raise credentials.SecretNotFound('github')

    monkeypatch.setattr(ride.scope.credentials, 'build_scoped_store', missing)

    def boom(*_a, **_k):
      raise AssertionError('must not launch when hydration fails')

    monkeypatch.setattr(ride_session, '_host_session', boom)
    assert ride_session.start_session(_spec(name='fresh', host=True)) == 1
    assert not (tmp_path / 'var' / 'ride' / 'workspaces' / 'fresh').exists()

  def test_resume_guard_fails_fast_before_worktree_create(self, monkeypatch, tmp_path):
    workspace, _ = self._fake_workspace(monkeypatch, tmp_path, has_session=False)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(ride_session.os, 'chdir', lambda p: None)

    def boom(*_a, **_k):
      raise AssertionError('must not create a worktree for a resume with no session')

    monkeypatch.setattr(ride_session, 'ensure_host_worktree', boom)
    assert self._host_session(_spec(host=True, resume=True), workspace, _launch_scope()) == 1


class TestHostBrokerPingRoundTrip:
  """the host broker channel, live: the runner process a host session spawns gets a
  provisioned channel in its env, and `broker request ping` receives a correlated
  pong over it — real socket, real broker loop, real CLI subprocess. Only the
  worktree git plumbing is stubbed (covered by its own tests); the claude-state
  provisioning runs for real against a fake HOME, so the test neither depends on
  the machine's claude login nor writes into the real ~/.claude."""

  def test_broker_request_ping_from_a_host_session(self, monkeypatch, capfd, tmp_path):
    root = tmp_path
    monkeypatch.setenv('XDG_DATA_HOME', str(root / 'state'))
    home = root / 'home'
    home.mkdir()
    # the identity fields _seed_claude_json requires from the host ~/.claude.json
    (home / '.claude.json').write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'u'}))
    monkeypatch.setenv('HOME', str(home))
    workspace = Workspace.create('w', root, WorkspaceKind.WORKTREE)
    workspace.tree.mkdir(parents=True)
    runtime_bundle = _runtime_bundle(root)
    ride_binary = runtime_bundle.host_venv / 'bin' / 'ride'
    ride_binary.write_text('#!/bin/sh\nexec broker request ping "{}" --timeout 30\n')

    monkeypatch.setattr(ride_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(ride_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(ride_session, 'provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(ride.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set())
    )
    monkeypatch.setattr(
      ride.scope.credentials,
      'build_scoped_store',
      lambda store, names, optional=(): (_scoped_store(), frozenset()),
    )
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    assert (
      ride_session._launch_session(
        _spec(host=True),
        workspace,
        None,
        _launch_scope(),
        human_env={},
        container=False,
        runtime_bundle=runtime_bundle,
        container_runtime=ContainerRuntimeResolver.fixed(
          ContainerRuntime('runtime-image', 'bundle-hash')
        ),
      )
      == 0
    )
    # the CLI printed the correlated reply's wire JSON
    assert '"outcome": "ok"' in capfd.readouterr().out
    # the channel socket is unlinked once the root exits
    assert list((root / 'var' / 'ride' / 'broker').glob('*.sock')) == []
    # the session claude state landed in the workspace, seeded from its identity
    seeded = workspace.path / 'claude' / '.claude.json'
    assert json.loads(seeded.read_text())['userID'] == 'u'


class TestManualSummonRoundTrip:
  """a manual summon, live: the root session registers it over a real broker,
  an external process discovers the pending record, attaches to the provisioned
  channel as the child, and the blocking summon collects its answer — the whole
  expected-peer path with no docker and no claude."""

  _ANSWER_CHILD = """
import json, os, sys, time
from pathlib import Path
from bro.broker import brotocol
from bro.broker.transport import connect
from bro.broker.transports.tcp import LOCAL_HOST, Endpoint

pending_dir = Path(sys.argv[1])
os.environ['XDG_DATA_HOME'] = str(pending_dir.parent.parent.parent)
deadline = time.time() + 15
records = []
while time.time() < deadline:
  records = list(pending_dir.glob('*.json'))
  if records:
    break
  time.sleep(0.05)
if not records:
  sys.exit(3)
record = json.loads(records[0].read_text())
from ride import pending_summon
pending_summon.claim(record['token'], workspace='external-ws')
client = connect(Endpoint(port=record['port'], token=record['channel_token']).address(LOCAL_HOST))
exchange = record['token']
client.send(brotocol.progress(exchange, {'trail_id': 't-manual'}))
client.send(brotocol.result(exchange, 'ok', value='the pair verdict'))
client.close(confirm=True)
"""

  def test_manual_summon_round_trip_from_a_host_session(self, monkeypatch, capfd, tmp_path):
    from bro.monitor import trail_pointer as trail_pointer_module
    from bro.workspace.paths import summon_dir, workspace_dir

    root = tmp_path
    monkeypatch.setenv('XDG_DATA_HOME', str(root / 'state'))
    home = root / 'home'
    home.mkdir()
    (home / '.claude.json').write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'u'}))
    monkeypatch.setenv('HOME', str(home))
    workspace = Workspace.create('w', root, WorkspaceKind.WORKTREE)
    workspace.tree.mkdir(parents=True)
    answer_child = root / 'answer_child.py'
    answer_child.write_text(self._ANSWER_CHILD)
    pending_dir = summon_dir() / 'pending'
    runtime_bundle = _runtime_bundle(root)
    ride_binary = runtime_bundle.host_venv / 'bin' / 'ride'
    # stands in for the in-place runner: register the manual summon, let the
    # external child answer it, and block for the relayed answer
    ride_binary.write_text(
      f'#!/bin/sh\n{shlex.quote(sys.executable)} {shlex.quote(str(answer_child))} '
      f'{shlex.quote(str(pending_dir))} &\n'
      "exec summon --manual bro-dev 'pair on this'\n"
    )
    ride_binary.chmod(0o755)

    monkeypatch.setattr(ride_session.os, 'chdir', lambda p: None)
    monkeypatch.setattr(ride_session, 'ensure_host_worktree', lambda *_a: True)
    monkeypatch.setattr(ride_session, 'provision_host_worktree', lambda *_a: True)
    monkeypatch.setattr(ride.summon_control, 'summon_allow_list', lambda *_a, **_k: set())
    monkeypatch.setattr(credentials, 'try_get', lambda name: 'tok')
    monkeypatch.setattr(
      ride_session, 'scoped_secrets', lambda *_a, **_k: ScopedSecrets(set(), set())
    )
    monkeypatch.setattr(
      ride.scope.credentials,
      'build_scoped_store',
      lambda store, names, optional=(): (_scoped_store(), frozenset()),
    )
    monkeypatch.delenv('BROKER_DISABLED', raising=False)
    assert (
      ride_session._launch_session(
        _spec(host=True),
        workspace,
        None,
        _launch_scope(may_summon={'bro-dev'}),
        human_env={},
        container=False,
        runtime_bundle=runtime_bundle,
        container_runtime=ContainerRuntimeResolver.fixed(
          ContainerRuntime('runtime-image', 'bundle-hash')
        ),
      )
      == 0
    )
    # the blocking summon relayed the external child's answer
    assert 'the pair verdict' in capfd.readouterr().out
    # the summon ended: both token records are discarded and the ledger carries ok
    assert list(pending_dir.glob('*.json')) == []
    assert list((summon_dir() / 'claimed').glob('*.json')) == []
    status = json.loads((summon_dir() / 'w.status.json').read_text())
    assert status['active'] == []
    assert status['last']['outcome'] == 'ok'
    assert status['last']['trail_id'] == 't-manual'
    # the claimed workspace routed the trail pointer to the child's own
    # (user-chosen) workspace
    pointer = trail_pointer_module.session_pointer(workspace_dir('external-ws'))
    assert trail_pointer_module.read(pointer) == 't-manual'


def _pending_record(tmp_path, **overrides) -> pending_summon.PendingSummon:
  record = pending_summon.PendingSummon(
    **{
      'token': 'TOK-1',
      'port': 7321,
      'channel_token': 'tk',
      'target': 'bro-dev',
      'prompt': 'pair on this',
      'parent_workspace': str(tmp_path / 'parent-tree'),
      'may_summon': ('dev',),
      'grant': (),
      'revoke': (),
      'summoner': {'trail_id': 'T1'},
      **overrides,
    }
  )
  pending_summon.write(record)
  return record


class TestSummonedSession:
  def test_container_summoned_launch_attaches_to_the_summoners_channel(self, tmp_path):
    record = _pending_record(tmp_path)
    with (
      _ContainerHarness() as h,
      patch('ride.session.run_summoned_in_container', return_value=0) as run,
      patch('ride.session.broker_enabled', return_value=True),
      patch('ride.session.resolve_head', return_value='parentsha') as head,
    ):
      rc = ride_session.start_session(_spec(prompt='pair on this'), summoned=record)
    assert rc == 0
    assert h.run_in_container.call_count == 0  # no broker of its own
    assert head.call_args.args == (tmp_path, ride_session.Path(record.parent_workspace))
    launch = run.call_args.args[0]
    assert launch.env['BROKER_CHANNEL'] == 'tcp://tk@host.docker.internal:7321'
    assert launch.env['RIDE_SUMMONED'] == '1'
    assert launch.env['RIDE_MAY_SUMMON'] == 'dev'
    assert launch.env['RIDE_WORKSPACE'] == 'w'
    assert json.loads(launch.env['RIDE_SUMMONER']) == {'trail_id': 'T1'}
    assert launch.env['RIDE_BASE_REF'] == 'parentsha'
    assert launch.tty
    # the threaded claim consumes the token
    run.call_args.kwargs['claim']()
    with pytest.raises(pending_summon.UnknownToken):
      pending_summon.peek(record.token)

  def test_summon_into_overrides_the_parent_head(self, tmp_path):
    record = _pending_record(tmp_path, into='release')
    with (
      _ContainerHarness(),
      patch('ride.session.run_summoned_in_container', return_value=0) as run,
      patch('ride.session.broker_enabled', return_value=True),
      patch('ride.session.resolve_ref', return_value='intosha') as ref,
    ):
      rc = ride_session.start_session(_spec(), summoned=record)
    assert rc == 0
    assert ref.call_args.args == (tmp_path, 'release')
    assert run.call_args.args[0].env['RIDE_BASE_REF'] == 'intosha'

  def test_container_summoned_requires_the_broker_channel(self, tmp_path, caplog):
    record = _pending_record(tmp_path)
    with (
      _ContainerHarness() as h,
      patch('ride.session.broker_enabled', return_value=False),
    ):
      rc = ride_session.start_session(_spec(), summoned=record)
    assert rc == 1
    assert h.run_in_container.call_count == 0
    assert "needs the summoner's broker channel" in caplog.text

  def test_unreadable_parent_head_fails_the_launch(self, tmp_path, caplog):
    record = _pending_record(tmp_path)
    with (
      _ContainerHarness() as h,
      patch('ride.session.broker_enabled', return_value=True),
      patch('ride.session.resolve_head', return_value=None),
    ):
      rc = ride_session.start_session(_spec(), summoned=record)
    assert rc == 1
    assert h.run_in_container.call_count == 0
    assert "cannot read the summoner's HEAD" in caplog.text
