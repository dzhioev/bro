from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ride.bro as bro_harness
import ride.session as ride_session
from bro.llm.llms.openai import LLMSpec
from bro.monitor import trail_pointer
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.store import ScopedSecrets
from ride.identity import bro_git_identity_env
from ride.session import ScopedLaunch, SessionSpec


def _spec(**overrides) -> SessionSpec:
  values = {
    'name': 'w',
    'harness': 'bro',
    'workspace_pinned': True,
    'host': False,
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


def _scope(**overrides) -> ScopedLaunch:
  values = {
    'scoped': ScopedSecrets({'openai'}, {'trails'}, False),
    'may_summon': {'reviewer'},
    'store': {'credentials.json': b'{}'},
  }
  values.update(overrides)
  return ScopedLaunch(**values)


@pytest.fixture(autouse=True)
def local_trails(monkeypatch):
  # keep the launch composition off the machine's own trails credential
  monkeypatch.setattr(ride_session, 'local_trails_mounts', lambda scoped: ())


class TestInnerCommand:
  def test_chat_uses_the_bro_in_place_runner(self, tmp_path):
    spec = _spec(llm='openai:fable:high+fast')
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    assert bro_harness.BRO.inner_command(spec, workspace) == [
      'bro',
      'chat',
      'dev',
      'start here',
      '--llm',
      'openai:fable:high+fast',
      '--hold',
      'attended',
      '--in-place',
    ]

  def test_forwarded_arguments_splice_into_the_native_argv(self, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    command = bro_harness.BRO.inner_command(_spec(arguments=['--fork']), workspace)
    assert command == [
      'bro', 'chat', 'dev', 'start here', '--hold', 'attended', '--fork', '--in-place'
    ]  # fmt: skip

  def test_resume_carries_the_workspace_trail_and_recorded_recipe(self, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    trail_pointer.write(trail_pointer.broker_pointer(workspace.path), 'trail-1')
    command = bro_harness.BRO.inner_command(_spec(resume=True, prompt=None), workspace)
    assert command[:3] == ['bro', 'chat', 'dev']
    assert command[command.index('--continue-trail') + 1] == 'trail-1'
    assert '"type":"openai"' in command[command.index('--continue-llm') + 1]


class TestContainerSession:
  def test_composes_the_bro_run_launch(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    captured: dict = {}
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)

    def run(launch, run_workspace, **kwargs):
      captured['launch'] = launch
      captured['workspace'] = run_workspace
      captured.update(kwargs)
      return 7

    monkeypatch.setattr(ride_session, 'run_in_container', run)
    spec = _spec(solo=True, hold='unattended')
    assert ride_session._launch_session(spec, workspace, 'abc123', _scope(), container=True) == 7
    launch = captured['launch']
    assert launch.command == [
      'bro', 'run', 'dev', 'start here', '--hold', 'unattended', '--in-place'
    ]  # fmt: skip
    assert launch.env == {
      'RIDE_BRO': 'dev',
      'RIDE_BASE_REF': 'abc123',
      **bro_git_identity_env('dev'),
    }
    assert not launch.tty
    assert captured['workspace'] is workspace
    assert captured['may_summon'] == {'reviewer'}

  def test_no_trails_disables_recording_in_the_container_env(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    captured: dict = {}
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)
    monkeypatch.setattr(
      ride_session,
      'run_in_container',
      lambda launch, *_a, **_k: captured.update(launch=launch) or 0,
    )
    spec = _spec(no_trails=True)
    assert ride_session._launch_session(spec, workspace, None, _scope(), container=True) == 0
    assert captured['launch'].env['TRAILS_DISABLED'] == '1'
    assert captured['launch'].extra_mounts == ()

  def test_resume_refuses_without_a_broker_published_pointer(self, caplog, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    assert (
      ride_session._launch_session(
        _spec(resume=True, prompt=None), workspace, None, _scope(), container=True
      )
      == 1
    )
    assert 'no trail pointer was published' in caplog.text

  def test_fresh_session_clears_a_stale_pointer(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    pointer = trail_pointer.broker_pointer(workspace.path)
    trail_pointer.write(pointer, 'stale')
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: None)
    monkeypatch.setattr(ride_session, 'run_in_container', lambda *_a, **_k: 0)
    assert ride_session._launch_session(_spec(), workspace, None, _scope(), container=True) == 0
    assert not pointer.exists()

  def test_a_refused_second_launch_leaves_the_active_pointer_alone(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    pointer = trail_pointer.broker_pointer(workspace.path)
    trail_pointer.write(pointer, 'live')
    monkeypatch.setattr(ride_session, 'find_container_id', lambda _tree: 'active')
    assert ride_session._launch_session(_spec(), workspace, None, _scope(), container=True) == 1
    assert trail_pointer.read(pointer) == 'live'


class TestHostSession:
  def _workspace(self, tmp_path: Path) -> tuple[Workspace, Path]:
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.WORKTREE)
    bro_binary = workspace.tree / '.venv' / 'bin' / 'bro'
    bro_binary.parent.mkdir(parents=True)
    bro_binary.write_text('')
    return workspace, bro_binary

  def _prepare(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ride_session, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(ride_session.os, 'chdir', lambda _path: None)
    monkeypatch.setattr(ride_session, 'ensure_host_worktree', lambda *_args: True)
    monkeypatch.setattr(ride_session, 'provision_host_worktree', lambda *_args: True)
    monkeypatch.setattr(
      ride_session, 'materialize_scoped_store', lambda _store, path: path / 'credentials.json'
    )

  def test_provisions_and_supervises_the_worktree_runner(self, monkeypatch, tmp_path):
    workspace, bro_binary = self._workspace(tmp_path)
    self._prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: True)
    root = MagicMock(return_value=3)
    monkeypatch.setattr(ride_session, 'run_host_process_via_broker', root)

    assert (
      ride_session._launch_session(_spec(host=True), workspace, None, _scope(), container=False)
      == 3
    )
    command = root.call_args.args[1]
    env = root.call_args.args[2]
    assert command == [
      str(bro_binary), 'chat', 'dev', 'start here', '--hold', 'attended', '--in-place'
    ]  # fmt: skip
    assert env['RIDE_BRO'] == 'dev'
    assert env[ride_session.START_SESSION_BROXY_ENV] == '1'
    assert env['GIT_AUTHOR_NAME'] == 'dev'
    assert env['GIT_AUTHOR_EMAIL'] == 'dev@bro'
    assert workspace.is_clean() == (False, ['last session exited with code 3'])

  def test_brokerless_host_run_unsets_an_ambient_channel(self, monkeypatch, tmp_path):
    workspace, _ = self._workspace(tmp_path)
    self._prepare(monkeypatch, tmp_path)
    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/ambient.sock')
    monkeypatch.setattr(ride_session, 'broker_enabled', lambda: False)
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(ride_session.subprocess, 'run', run)

    assert (
      ride_session._launch_session(_spec(host=True), workspace, None, _scope(), container=False)
      == 0
    )
    assert 'BROKER_CHANNEL' not in run.call_args.kwargs['env']
    assert ride_session.START_SESSION_BROXY_ENV not in run.call_args.kwargs['env']
