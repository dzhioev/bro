from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ride.bro as bro_harness
import ride.session as ride_session
from bro.llm.llms.openai import LLMSpec
from bro.monitor import SESSION_DIR_ENV, trail_pointer, workspace_party_dir, workspace_session_dir
from bro.workspace.paths import CONTAINER_PARTY_DIR, CONTAINER_SESSION_DIR
from ride.runtime_bundle import RuntimeBundle
from ride.session import ScopedLaunch, SessionSpec
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets


def _materialize_store(_store, directory: Path) -> Path:
  """The empty scoped store a host launch materializes."""
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'creds.json').write_text('{}')
  return directory


def _spec(**overrides) -> SessionSpec:
  values = {
    'name': 'w',
    'harness': 'bro',
    'workspace_pinned': True,
    'isolation': Isolation.BOXED,
    'drop': False,
    'hold': 'attended',
    'grant': [],
    'revoke': [],
    'llm': None,
    'resolved_llm': LLMSpec().dump(),
    'solo': False,
    'resume': False,
    'no_trails': False,
    'into': None,
    'bro': 'dev',
    'prompt': 'start here',
    'subject': 'start here',
    'arguments': [],
    'harness_options': {},
  }
  values.update(overrides)
  return SessionSpec(**values)


def _runtime_bundle(tmp_path: Path) -> RuntimeBundle:
  return RuntimeBundle(tmp_path / 'runtime-bundle', '3.12')


def _container_runtime() -> ContainerRuntimeResolver:
  return ContainerRuntimeResolver.fixed(ContainerRuntime('runtime-image', 'bundle-hash'))


def _scope(**overrides) -> ScopedLaunch:
  values = {
    'scoped': ScopedSecrets({'openai'}, {'trails'}),
    'may_summon': {'reviewer'},
    'permits': {'party.start.boxed'},
    'store': {'creds.json': b'{}'},
  }
  values.update(overrides)
  return ScopedLaunch(**values)


@pytest.fixture(autouse=True)
def local_trails(monkeypatch):
  # keep the launch composition off the machine's own trails credential
  monkeypatch.setattr(ride_session, 'local_trails_mounts', lambda scoped: ())


class TestNativeArgv:
  """The argv the bro harness runner spawns."""

  def _argv(self, spec, monkeypatch) -> list[str]:
    spawned: list[list[str]] = []
    monkeypatch.setattr(bro_harness.shutil, 'which', lambda command: f'/venv/bin/{command}')
    monkeypatch.setattr(bro_harness, 'run_agent', lambda argv: spawned.append(argv) or 0)
    assert bro_harness.BRO.run_session(spec) == 0
    return spawned[0]

  def _session_dir(self, monkeypatch, tmp_path) -> Path:
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    session = workspace_session_dir(workspace.path)
    monkeypatch.setenv(SESSION_DIR_ENV, str(session))
    return session

  def test_chat_composes_the_native_argv(self, monkeypatch):
    spec = _spec(llm='openai:fable:high+fast')
    assert self._argv(spec, monkeypatch) == [
      'bro',
      'chat',
      'dev',
      'start here',
      '--llm',
      'openai:fable:high+fast',
      '--hold',
      'attended',
    ]

  def test_forwarded_arguments_splice_into_the_native_argv(self, monkeypatch):
    argv = self._argv(_spec(arguments=['--fork']), monkeypatch)
    assert argv == ['bro', 'chat', 'dev', 'start here', '--hold', 'attended', '--fork']

  def test_resume_carries_the_session_trail_and_recorded_recipe(self, monkeypatch, tmp_path):
    session = self._session_dir(monkeypatch, tmp_path)
    trail_pointer.write(session / trail_pointer.FILENAME, 'trail-1')
    argv = self._argv(_spec(resume=True, prompt=None), monkeypatch)
    assert argv[:3] == ['bro', 'chat', 'dev']
    assert argv[argv.index('--continue-trail') + 1] == 'trail-1'
    assert '"type":"openai"' in argv[argv.index('--continue-llm') + 1]

  def test_resume_without_a_published_pointer_fails(self, monkeypatch, tmp_path, caplog):
    self._session_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(bro_harness.shutil, 'which', lambda command: f'/venv/bin/{command}')
    assert bro_harness.BRO.run_session(_spec(resume=True)) == 1
    assert 'no bro harness trail recorded' in caplog.text

  def test_missing_native_distribution_fails_before_spawn(self, monkeypatch, caplog):
    run_agent = MagicMock()
    monkeypatch.setattr(bro_harness.shutil, 'which', lambda _command: None)
    monkeypatch.setattr(bro_harness, 'run_agent', run_agent)

    assert bro_harness.BRO.run_session(_spec()) == 1
    assert 'install bro-native' in caplog.text
    run_agent.assert_not_called()


class TestContainerSession:
  def test_composes_the_bro_run_launch(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    captured: dict = {}
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)

    def run(launch, run_workspace, **kwargs):
      captured['launch'] = launch
      captured['workspace'] = run_workspace
      captured.update(kwargs)
      return 7

    monkeypatch.setattr(ride_session, 'run_started_party', run)
    spec = _spec(solo=True, hold='unattended')
    assert (
      ride_session._launch_session(
        spec,
        workspace,
        'abc123',
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 7
    )
    launch = captured['launch']
    assert launch.command == [
      'do-ride', 'solo', '--workspace', 'w', '--harness', 'bro',
      '--hold', 'unattended', 'dev', 'start here',
    ]  # fmt: skip
    assert launch.env == {
      'RIDE_BRO': 'dev',
      'RIDE_ISOLATION': 'boxed',
      'RIDE_BRANCH': 'workspace-w',
      ride_session.RUNTIME_ENV: str(_runtime_bundle(tmp_path).host_root),
      ride_session.INSTALL_DIRECTORY_ENV: ride_session.CONTAINER_INSTALL_DIRECTORY,
      ride_session.RESOLVED_LLM_ENV: ride_session.encode_resolved_llm(spec.resolved_llm),
      'RIDE_SESSION_DIR': str(CONTAINER_SESSION_DIR),
    }
    assert launch.base_ref == 'abc123'
    assert not launch.tty
    assert captured['workspace'] is workspace
    assert captured['may_summon'] == {'reviewer'}

  def test_no_trails_disables_recording_in_the_container_env(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    captured: dict = {}
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)
    monkeypatch.setattr(
      ride_session,
      'run_started_party',
      lambda launch, *_a, **_k: captured.update(launch=launch) or 0,
    )
    spec = _spec(no_trails=True)
    assert (
      ride_session._launch_session(
        spec,
        workspace,
        None,
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 0
    )
    assert captured['launch'].env['TRAILS_DISABLED'] == '1'
    # the session state dir is not trails data — it stays mounted
    assert captured['launch'].extra_mounts == (
      f'{workspace_session_dir(workspace.path)}:{CONTAINER_SESSION_DIR}',
      f'{workspace_party_dir(workspace.path)}:{CONTAINER_PARTY_DIR}',
    )

  def test_resume_refuses_without_a_broker_published_pointer(self, caplog, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    assert (
      ride_session._launch_session(
        _spec(resume=True, prompt=None),
        workspace,
        None,
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 1
    )
    assert 'no trail pointer was published' in caplog.text

  def test_fresh_session_clears_a_stale_pointer(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    pointer = trail_pointer.session_pointer(workspace.path)
    trail_pointer.write(pointer, 'stale')
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)
    monkeypatch.setattr(ride_session, 'run_started_party', lambda *_a, **_k: 0)
    assert (
      ride_session._launch_session(
        _spec(),
        workspace,
        None,
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 0
    )
    assert not pointer.exists()

  def test_a_refused_second_launch_leaves_the_active_pointer_alone(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.BOXED)
    pointer = trail_pointer.session_pointer(workspace.path)
    trail_pointer.write(pointer, 'live')
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: 'active')
    assert (
      ride_session._launch_session(
        _spec(),
        workspace,
        None,
        _scope(),
        human_env={},
        runtime_bundle=_runtime_bundle(tmp_path),
        container_runtime=_container_runtime(),
      )
      == 1
    )
    assert trail_pointer.read(pointer) == 'live'


class TestUnboxedSession:
  def test_builder_prepares_the_native_snapshot_runner(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, Isolation.UNBOXED)
    root = tmp_path / 'runtime-bundle'
    (root / 'host' / 'venv' / 'bin').mkdir(parents=True)
    (root / 'host' / 'bin').mkdir()
    (root / 'host' / '.complete').touch()
    (root / 'host' / 'venv' / 'bin' / 'do-ride').touch()
    runtime_bundle = RuntimeBundle(root, '3.12')
    monkeypatch.setattr(ride_session, 'ensure_clone', lambda *_args: True)
    monkeypatch.setattr(ride_session, 'provision_workspace', lambda *_args: True)
    monkeypatch.setattr(ride_session, 'materialize_scoped_store', _materialize_store)

    launch = ride_session.started_party_launch(
      _spec(isolation=Isolation.UNBOXED, repo=str(tmp_path)),
      workspace,
      workspace.repository,
      None,
      _scope(),
      human_env={},
      runtime_bundle=runtime_bundle,
      container_runtime=_container_runtime(),
      forward_env=True,
      env={},
      credential_directory=workspace.path / 'credentials',
      install_directory=workspace.path / 'environment',
    )

    assert isinstance(launch, ride_session.ProcessLaunch)
    assert launch.command == [
      str(runtime_bundle.host_venv / 'bin' / 'do-ride'),
      'along',
      '--workspace',
      'w',
      '--harness',
      'bro',
      '--repo',
      str(tmp_path),
      '--hold',
      'attended',
      'dev',
      'start here',
    ]
    assert launch.env['BRO_INSTALL_KINDS'] == ''
