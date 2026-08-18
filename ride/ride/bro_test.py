from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ride.bro as bro_harness
import ride.bro_session as bro_session
from bro.launch.identity import bro_git_identity_env
from bro.llm.llms.openai import LLMSpec
from bro.monitor import trail_pointer
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import Workspace
from bro.workspace.store import ScopedSecrets
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
    'into': None,
    'bro': 'dev',
    'prompt': 'start here',
    'harness_options': bro_harness.BroOptions(
      rich=False, text=False, no_trails=False, subject='start here'
    ).dump(),
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


class TestBroOptions:
  def test_round_trips(self):
    options = bro_harness.BroOptions(rich=True, text=False, no_trails=True, subject='do the work')
    assert bro_harness.BroOptions.load(options.dump()) == options

  def test_rejects_an_unexpected_shape(self):
    with pytest.raises(ValueError, match='unexpected bro option fields'):
      bro_harness.BroOptions.load({'rich': True})

  def test_no_trails_removes_the_scope_baseline(self):
    spec = _spec(
      harness_options=bro_harness.BroOptions(
        rich=False, text=False, no_trails=True, subject=None
      ).dump()
    )
    assert bro_harness.BRO.scope_recipe(spec).optional_baseline == frozenset()


class TestInnerCommand:
  def test_chat_uses_the_bro_in_place_runner(self):
    spec = _spec(
      llm='openai:fable:high+fast',
      harness_options=bro_harness.BroOptions(
        rich=False, text=True, no_trails=False, subject='start here'
      ).dump(),
    )
    assert bro_session.inner_command(spec) == [
      'bro',
      'chat',
      'dev',
      'start here',
      '--text',
      '--llm',
      'openai:fable:high+fast',
      '--hold',
      'attended',
      '--in-place',
    ]

  def test_resume_carries_the_workspace_trail_and_recorded_recipe(self, monkeypatch, tmp_path):
    monkeypatch.setattr(bro_session, 'session_trail_pointer', lambda _ws: tmp_path / 'pointer')
    trail_pointer.write(tmp_path / 'pointer', 'trail-1')
    command = bro_session.inner_command(_spec(resume=True, prompt=None))
    assert command[:3] == ['bro', 'chat', 'dev']
    assert command[command.index('--continue-trail') + 1] == 'trail-1'
    assert '"type":"openai"' in command[command.index('--continue-llm') + 1]


class TestContainerSession:
  def test_composes_the_shared_bro_run_description(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    captured: dict = {}
    monkeypatch.setattr(bro_session, 'find_container_id', lambda _tree: None)

    def run(launch, **kwargs):
      captured['launch'] = launch
      captured.update(kwargs)
      return 7

    monkeypatch.setattr(bro_session, 'run_in_container', run)
    spec = _spec(
      solo=True,
      hold='unattended',
      harness_options=bro_harness.BroOptions(
        rich=True, text=False, no_trails=False, subject='start here'
      ).dump(),
    )
    assert bro_session.launch_session(spec, workspace, 'abc123', _scope(), container=True) == 7
    launch = captured['launch']
    assert launch.command == [
      'bro', 'run', 'dev', 'start here', '--rich', '--hold', 'unattended', '--in-place'
    ]  # fmt: skip
    assert launch.env == {
      'RIDE_BRO': 'dev',
      'RIDE_BASE_REF': 'abc123',
      **bro_git_identity_env('dev'),
    }
    assert not launch.tty
    assert captured['workspace'] is workspace
    assert captured['may_summon'] == {'reviewer'}
    assert captured['trail_pointer'] == workspace.path / trail_pointer.FILENAME

  def test_resume_refuses_without_a_broker_published_pointer(self, caplog, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    assert (
      bro_session.launch_session(
        _spec(resume=True, prompt=None), workspace, None, _scope(), container=True
      )
      == 1
    )
    assert 'no trail pointer was published' in caplog.text

  def test_fresh_session_clears_a_stale_pointer(self, monkeypatch, tmp_path):
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.CONTAINER)
    pointer = workspace.path / trail_pointer.FILENAME
    trail_pointer.write(pointer, 'stale')
    monkeypatch.setattr(bro_session, 'find_container_id', lambda _tree: 'active')
    assert bro_session.launch_session(_spec(), workspace, None, _scope(), container=True) == 1
    assert not pointer.exists()


class TestHostSession:
  def _workspace(self, tmp_path: Path) -> tuple[Workspace, Path]:
    workspace = Workspace.create('w', tmp_path, WorkspaceKind.WORKTREE)
    bro_binary = workspace.tree / '.venv' / 'bin' / 'bro'
    bro_binary.parent.mkdir(parents=True)
    bro_binary.write_text('')
    return workspace, bro_binary

  def test_provisions_and_supervises_the_worktree_runner(self, monkeypatch, tmp_path):
    workspace, bro_binary = self._workspace(tmp_path)
    monkeypatch.setattr(bro_session, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(bro_session.os, 'chdir', lambda _path: None)
    monkeypatch.setattr(bro_session, 'ensure_host_worktree', lambda *_args: True)
    monkeypatch.setattr(bro_session, 'provision_host_worktree', lambda *_args: True)
    monkeypatch.setattr(
      bro_session, 'materialize_scoped_store', lambda _store, path: path / 'credentials.json'
    )
    monkeypatch.setattr(bro_session, 'broker_enabled', lambda: True)
    root = MagicMock(return_value=3)
    monkeypatch.setattr(bro_session, 'run_host_process_via_broker', root)

    assert (
      bro_session.launch_session(_spec(host=True), workspace, None, _scope(), container=False) == 3
    )
    command = root.call_args.args[1]
    env = root.call_args.args[2]
    assert command == [
      str(bro_binary), 'chat', 'dev', 'start here', '--hold', 'attended', '--in-place'
    ]  # fmt: skip
    assert env['RIDE_BRO'] == 'dev'
    assert env[bro_session.START_SESSION_BROXY_ENV] == '1'
    assert env['GIT_AUTHOR_NAME'] == 'bro'
    assert env['GIT_AUTHOR_EMAIL'] == 'dev@bro'
    assert root.call_args.kwargs['trail_pointer'] == workspace.path / trail_pointer.FILENAME
    assert workspace.is_clean() == (False, ['last session exited with code 3'])

  def test_brokerless_host_run_unsets_an_ambient_channel(self, monkeypatch, tmp_path):
    workspace, _ = self._workspace(tmp_path)
    monkeypatch.setenv('BROKER_CHANNEL', 'unix:/ambient.sock')
    monkeypatch.setattr(bro_session, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(bro_session.os, 'chdir', lambda _path: None)
    monkeypatch.setattr(bro_session, 'ensure_host_worktree', lambda *_args: True)
    monkeypatch.setattr(bro_session, 'provision_host_worktree', lambda *_args: True)
    monkeypatch.setattr(
      bro_session, 'materialize_scoped_store', lambda _store, path: path / 'credentials.json'
    )
    monkeypatch.setattr(bro_session, 'broker_enabled', lambda: False)
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(bro_session.subprocess, 'run', run)

    assert (
      bro_session.launch_session(_spec(host=True), workspace, None, _scope(), container=False) == 0
    )
    assert 'BROKER_CHANNEL' not in run.call_args.kwargs['env']
    assert bro_session.START_SESSION_BROXY_ENV not in run.call_args.kwargs['env']
