import os
import subprocess
import sys

import pytest

from bro.workspace.paths import workspaces_dir
from ride.repository import Repository
from ride.workspace import model
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import (
  AttachmentMismatch,
  ContainerWorkspace,
  KindMismatch,
  Workspace,
  WorktreeWorkspace,
)


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


def _worktree(name: str, project) -> Workspace:
  return Workspace.create(name, project, WorkspaceKind.WORKTREE)


def _container(name: str, project) -> Workspace:
  return Workspace.create(name, project, WorkspaceKind.CONTAINER)


class TestCleanupImage:
  def test_prefers_current_tag_when_present(self, monkeypatch):
    monkeypatch.setattr(model, 'runtime_image_tag', lambda: 'example/session:cur')
    monkeypatch.setattr(model.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    assert model._cleanup_image(None) == 'example/session:cur'

  def test_falls_back_to_an_image_from_the_same_repository(self, monkeypatch):
    monkeypatch.setattr(model, 'runtime_image_tag', lambda: 'example/session:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':  # docker image inspect -> miss
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='example/session:<none>\nexample/session:abc123\n')

    monkeypatch.setattr(model.subprocess, 'run', fake_run)
    assert model._cleanup_image(None) == 'example/session:abc123'

  def test_none_when_no_image(self, monkeypatch):
    monkeypatch.setattr(model, 'runtime_image_tag', lambda: 'example/session:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='')

    monkeypatch.setattr(model.subprocess, 'run', fake_run)
    assert model._cleanup_image(None) is None


class TestRemoveContainerDir:
  def test_plain_rmtree_when_host_owned(self, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(model.shutil, 'rmtree', lambda p: calls.append(p))
    monkeypatch.setattr(
      model.subprocess, 'run', lambda *a, **k: pytest.fail('docker must not be invoked')
    )
    model._remove_container_dir(tmp_path / 'ws', image='example/session:x')
    assert calls == [tmp_path / 'ws']

  def test_missing_dir_is_noop(self, monkeypatch, tmp_path):
    def boom(_):
      raise FileNotFoundError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    monkeypatch.setattr(
      model.subprocess, 'run', lambda *a, **k: pytest.fail('docker must not be invoked')
    )
    model._remove_container_dir(tmp_path / 'gone', image='example/session:x')

  def test_escalates_to_root_container_on_eperm(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    seen = {}

    def fake_run(argv, *a, **k):
      seen['argv'] = argv
      return _FakeProc(returncode=0)

    monkeypatch.setattr(model.subprocess, 'run', fake_run)
    target = tmp_path / 'ws'  # never created -> path.exists() is False afterwards
    model._remove_container_dir(target, image='example/session:x')
    argv = seen['argv']
    assert argv[:5] == ['docker', 'run', '--rm', '-u', '0']
    assert '--entrypoint' in argv and argv[argv.index('--entrypoint') + 1] == 'rm'
    assert f'{tmp_path}:/target' in argv
    assert argv[-2:] == ['-rf', f'/target/{target.name}']

  def test_raises_when_no_image_available(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    with pytest.raises(RuntimeError, match='no session image'):
      model._remove_container_dir(tmp_path / 'ws', image=None)

  def test_raises_when_docker_rm_fails(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    monkeypatch.setattr(
      model.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=1, stderr='denied')
    )
    with pytest.raises(RuntimeError, match='docker rm failed: denied'):
      model._remove_container_dir(tmp_path / 'ws', image='example/session:x')

  def test_raises_when_dir_survives_docker_rm(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    monkeypatch.setattr(model.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    survivor = tmp_path / 'ws'
    survivor.mkdir()  # still present after the mocked docker rm
    with pytest.raises(RuntimeError, match='still present'):
      model._remove_container_dir(survivor, image='example/session:x')


class TestContainerWorkspaceRemove:
  def test_removes_the_whole_workspace_dir_with_the_cleanup_image(self, monkeypatch, tmp_path):
    monkeypatch.setattr(model, '_cleanup_image', lambda _repo: 'example/session:img')
    removed = {}
    monkeypatch.setattr(
      model,
      '_remove_container_dir',
      lambda path, image: removed.update(path=path, image=image),
    )
    workspace = _container('ws', tmp_path)
    workspace.record_session_end(0)
    workspace.remove()
    assert removed == {'path': workspace.path, 'image': 'example/session:img'}


class TestWorktreeWorkspaceRemove:
  def test_removes_worktree_branch_and_the_workspace_dir(self, monkeypatch, tmp_path):
    calls = []

    def fake_git_run(*args, **kwargs):
      calls.append(args)
      return _FakeProc()

    monkeypatch.setattr(model, 'git_run', fake_git_run)
    workspace = _worktree('ws', tmp_path)
    workspace.tree.mkdir(parents=True)
    workspace.record_session_end(0)
    workspace.host_log.write_text('mid-session line\n')
    workspace.remove()
    assert calls == [
      ('worktree', 'remove', '--force', str(workspace.tree)),
      ('branch', '-D', 'worktree-ws'),
    ]
    assert not workspace.path.exists()

  def test_a_never_materialized_tree_skips_the_git_removal(self, monkeypatch, tmp_path):
    calls = []

    def fake_git_run(*args, **kwargs):
      calls.append(args)
      return _FakeProc()

    monkeypatch.setattr(model, 'git_run', fake_git_run)
    workspace = _worktree('ws', tmp_path)
    workspace.remove()
    assert calls == [('branch', '-D', 'worktree-ws')]
    assert not workspace.path.exists()

  def test_a_failed_worktree_removal_raises(self, monkeypatch, tmp_path):
    monkeypatch.setattr(model, 'git_run', lambda *a, **k: _FakeProc(returncode=1, stderr='busy'))
    workspace = _worktree('ws', tmp_path)
    workspace.tree.mkdir(parents=True)
    with pytest.raises(RuntimeError, match='git worktree remove failed: busy'):
      workspace.remove()
    assert workspace.path.exists()


class TestSessionEndRecord:
  def test_record_and_clear_round_trip(self, tmp_path):
    workspace = _worktree('ws', tmp_path)
    workspace.record_session_end(0)
    assert (workspace.path / 'exit').read_text() == '0'
    workspace.clear_session_end()
    assert not (workspace.path / 'exit').exists()

  def test_clear_of_an_absent_record_is_a_noop(self, tmp_path):
    _worktree('ws', tmp_path).clear_session_end()

  def test_a_kill_is_recorded_without_a_code(self, tmp_path):
    workspace = _worktree('ws', tmp_path)
    workspace.record_session_end(None)
    assert (workspace.path / 'exit').read_text() == 'killed'


class TestIsClean:
  def test_clean_after_a_recorded_clean_exit(self, tmp_path):
    workspace = _worktree('ws', tmp_path)
    workspace.record_session_end(0)
    assert workspace.is_clean() == (True, [])

  def test_not_clean_without_a_record(self, tmp_path):
    assert _worktree('ws', tmp_path).is_clean() == (False, ['no recorded session end'])

  def test_not_clean_after_a_failed_session(self, tmp_path):
    workspace = _worktree('ws', tmp_path)
    workspace.record_session_end(3)
    assert workspace.is_clean() == (False, ['last session exited with code 3'])

  def test_not_clean_after_a_killed_session(self, tmp_path):
    workspace = _worktree('ws', tmp_path)
    workspace.record_session_end(None)
    assert workspace.is_clean() == (False, ['last session was killed'])


class TestSessionLock:
  def test_idle_workspace_is_inactive(self, tmp_path):
    assert _worktree('feat', tmp_path).is_active(set()) is False

  def test_held_lock_reads_as_active(self, tmp_path):
    workspace = _worktree('feat', tmp_path)
    with workspace.hold_session_lock():
      assert workspace.is_active(set()) is True
      assert workspace.lockfile.read_text() == str(os.getpid())
    assert workspace.is_active(set()) is False

  def test_a_second_holder_is_refused(self, tmp_path):
    workspace = _worktree('feat', tmp_path)
    with workspace.hold_session_lock():
      with pytest.raises(model.SessionBusy, match=f'feat.*pid {os.getpid()}'):
        with Workspace.open('feat').hold_session_lock():
          pass

  def test_the_lock_dies_with_its_holder(self, tmp_path):
    # flock releases on process death, so a killed session leaves nothing stale
    workspace = _worktree('feat', tmp_path)
    holder = subprocess.Popen(
      [
        sys.executable,
        '-c',
        'import fcntl,sys,time\n'
        'handle = open(sys.argv[1], "w")\n'
        'fcntl.flock(handle, fcntl.LOCK_EX)\n'
        'print("held", flush=True)\n'
        'time.sleep(60)\n',
        str(workspace.lockfile),
      ],
      stdout=subprocess.PIPE,
      text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == 'held'
    assert workspace.is_active(set()) is True
    holder.kill()
    holder.wait()
    assert workspace.is_active(set()) is False

  def test_container_active_when_its_tree_is_mounted(self, tmp_path):
    # a running container counts even with no launcher holding the lock
    workspace = _container('feat', tmp_path)
    assert workspace.is_active({str(workspace.tree)}) is True
    assert workspace.is_active(set()) is False


class TestDetachedWorkspace:
  def test_metadata_omits_repo_and_branch(self):
    workspace = Workspace.create('detached', None, WorkspaceKind.CONTAINER)
    assert workspace.metadata.dump() == {'kind': 'container', 'throwaway': False}
    assert workspace.repo is None
    assert workspace.metadata.branch is None

  def test_clean_means_the_tree_is_empty(self):
    workspace = Workspace.create('detached', None, WorkspaceKind.WORKTREE)
    assert workspace.is_clean() == (True, [])
    workspace.tree.mkdir()
    (workspace.tree / 'result').write_text('x')
    assert workspace.is_clean() == (False, ['detached workspace tree is not empty'])

  def test_url_attachment_is_recorded_as_the_url(self, tmp_path):
    repository = Repository('https://example.test/owner/repo.git', tmp_path / 'mirror', 'abc')
    workspace = Workspace.create('remote', repository, WorkspaceKind.CONTAINER)
    assert workspace.repo == repository.identity
    assert workspace.metadata.dump()['repo'] == repository.identity
    assert (
      Workspace.ensure('remote', repository, WorkspaceKind.CONTAINER).repo == repository.identity
    )

  def test_existing_workspace_refuses_a_different_attachment(self, tmp_path):
    Workspace.create('ws', tmp_path, WorkspaceKind.CONTAINER)
    with pytest.raises(AttachmentMismatch, match='not no repository'):
      Workspace.ensure('ws', None, WorkspaceKind.CONTAINER)

  def test_missing_attachment_requires_force_to_remove(self, monkeypatch, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    workspace = Workspace.create('ws', repo, WorkspaceKind.CONTAINER)
    repo.rmdir()
    monkeypatch.setattr(model, '_cleanup_image', lambda _repo: None)
    with pytest.raises(RuntimeError, match='no longer exists.*--force'):
      workspace.remove()
    workspace.remove(force=True)
    assert not workspace.path.exists()


class TestKindsAndEnumeration:
  def test_open_reads_the_recorded_kind(self, tmp_path):
    _worktree('h', tmp_path)
    _container('c', tmp_path)
    assert isinstance(Workspace.open('h'), WorktreeWorkspace)
    assert isinstance(Workspace.open('c'), ContainerWorkspace)

  def test_open_raises_for_an_unknown_name(self, tmp_path):
    with pytest.raises(ValueError, match='^workspace not found: gone$'):
      Workspace.open('gone')

  def test_create_records_kind_branch_and_throwaway(self, tmp_path):
    workspace = Workspace.create('ws', tmp_path, WorkspaceKind.CONTAINER, throwaway=True)
    reopened = Workspace.open('ws')
    assert reopened.metadata == workspace.metadata
    assert reopened.metadata.kind is WorkspaceKind.CONTAINER
    assert reopened.metadata.branch == 'worktree-ws'
    assert reopened.metadata.throwaway is True

  def test_ensure_returns_the_existing_workspace_of_the_same_kind(self, tmp_path):
    created = _container('ws', tmp_path)
    assert Workspace.ensure('ws', tmp_path, WorkspaceKind.CONTAINER).path == created.path

  def test_ensure_refuses_the_other_kind(self, tmp_path):
    _container('ws', tmp_path)
    with pytest.raises(KindMismatch, match='is a container workspace, not worktree'):
      Workspace.ensure('ws', tmp_path, WorkspaceKind.WORKTREE)

  def test_all_enumerates_one_namespace(self, tmp_path):
    _worktree('h1', tmp_path)
    _worktree('h2', tmp_path)
    _container('c1', tmp_path)
    listed = {workspace.name: workspace.kind for workspace in Workspace.all()}
    assert listed == {
      'h1': WorkspaceKind.WORKTREE,
      'h2': WorkspaceKind.WORKTREE,
      'c1': WorkspaceKind.CONTAINER,
    }

  def test_all_ignores_a_directory_that_records_no_workspace(self, tmp_path):
    _worktree('h1', tmp_path)
    (workspaces_dir() / 'leftover').mkdir()
    assert [workspace.name for workspace in Workspace.all()] == ['h1']

  def test_all_ignores_a_directory_whose_name_is_no_workspace_name(self, tmp_path):
    _worktree('h1', tmp_path)
    (workspaces_dir() / 'ride-\nstray prompt text').mkdir()
    assert [workspace.name for workspace in Workspace.all()] == ['h1']
