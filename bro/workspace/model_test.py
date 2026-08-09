import os
import subprocess
import sys

import pytest

from bro.workspace import model
from bro.workspace.model import ContainerWorkspace, HostWorktree, Workspace


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestCleanupImage:
  def test_prefers_current_tag_when_present(self, monkeypatch):
    monkeypatch.setattr(model, 'image_tag', lambda: 'example/session:cur')
    monkeypatch.setattr(model.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    assert model._cleanup_image() == 'example/session:cur'

  def test_falls_back_to_an_image_from_the_same_repository(self, monkeypatch):
    monkeypatch.setattr(model, 'image_tag', lambda: 'example/session:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':  # docker image inspect -> miss
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='example/session:<none>\nexample/session:abc123\n')

    monkeypatch.setattr(model.subprocess, 'run', fake_run)
    assert model._cleanup_image() == 'example/session:abc123'

  def test_none_when_no_image(self, monkeypatch):
    monkeypatch.setattr(model, 'image_tag', lambda: 'example/session:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='')

    monkeypatch.setattr(model.subprocess, 'run', fake_run)
    assert model._cleanup_image() is None


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
  def test_removes_dir_with_cleanup_image_and_host_log(self, monkeypatch, tmp_path):
    monkeypatch.setattr(model, 'containers_dir', lambda project: tmp_path / 'containers')
    monkeypatch.setattr(model, '_cleanup_image', lambda: 'example/session:img')
    removed = {}
    monkeypatch.setattr(
      model,
      '_remove_container_dir',
      lambda path, image: removed.update(path=path, image=image),
    )
    workspace = ContainerWorkspace('ws', tmp_path / 'project')
    host_log = tmp_path / 'project' / 'var' / 'cw' / 'log' / 'c:ws.log'
    host_log.parent.mkdir(parents=True)
    host_log.write_text('mid-session line\n')
    model.record_session_end(tmp_path / 'project', workspace.ref, 0)
    workspace.remove()
    assert removed == {'path': workspace.path, 'image': 'example/session:img'}
    # the session host log and end record go with the workspace
    assert not host_log.exists()
    assert workspace.is_clean() == (False, ['no recorded session end'])

  def test_host_log_cleaned_even_when_dir_removal_raises(self, monkeypatch, tmp_path):
    monkeypatch.setattr(model, 'containers_dir', lambda project: tmp_path / 'containers')
    monkeypatch.setattr(model, '_cleanup_image', lambda: None)

    def boom(path, image):
      raise RuntimeError('no image')

    monkeypatch.setattr(model, '_remove_container_dir', boom)
    workspace = ContainerWorkspace('ws', tmp_path / 'project')
    host_log = tmp_path / 'project' / 'var' / 'cw' / 'log' / 'c:ws.log'
    host_log.parent.mkdir(parents=True)
    host_log.write_text('mid-session line\n')
    with pytest.raises(RuntimeError, match='no image'):
      workspace.remove()
    assert not host_log.exists()


class TestHostWorktreeRemove:
  def test_removes_worktree_branch_and_host_log(self, monkeypatch, tmp_path):
    calls = []

    def fake_git_run(*args, **kwargs):
      calls.append(args)
      return _FakeProc()

    monkeypatch.setattr(model, 'git_run', fake_git_run)
    workspace = HostWorktree('ws', tmp_path / 'project')
    host_log = tmp_path / 'project' / 'var' / 'cw' / 'log' / 'ws.log'
    host_log.parent.mkdir(parents=True)
    host_log.write_text('mid-session line\n')
    model.record_session_end(tmp_path / 'project', workspace.ref, 0)
    workspace.remove()
    assert calls == [
      ('worktree', 'remove', '--force', str(workspace.path)),
      ('branch', '-D', 'worktree-ws'),
    ]
    assert not host_log.exists()
    assert workspace.is_clean() == (False, ['no recorded session end'])


class TestSessionEndRecord:
  def test_record_and_clear_round_trip(self, tmp_path):
    model.record_session_end(tmp_path, 'ws', 0)
    assert (tmp_path / 'var' / 'cw' / 'exit' / 'ws').read_text() == '0'
    model.clear_session_end(tmp_path, 'ws')
    assert not (tmp_path / 'var' / 'cw' / 'exit' / 'ws').exists()

  def test_clear_of_an_absent_record_is_a_noop(self, tmp_path):
    model.clear_session_end(tmp_path, 'ws')

  def test_a_kill_is_recorded_without_a_code(self, tmp_path):
    model.record_session_end(tmp_path, 'ws', None)
    assert (tmp_path / 'var' / 'cw' / 'exit' / 'ws').read_text() == 'killed'


class TestIsClean:
  def test_clean_after_a_recorded_clean_exit(self, tmp_path):
    model.record_session_end(tmp_path, 'ws', 0)
    assert HostWorktree('ws', tmp_path).is_clean() == (True, [])

  def test_not_clean_without_a_record(self, tmp_path):
    assert HostWorktree('ws', tmp_path).is_clean() == (False, ['no recorded session end'])

  def test_not_clean_after_a_failed_session(self, tmp_path):
    model.record_session_end(tmp_path, 'ws', 3)
    assert HostWorktree('ws', tmp_path).is_clean() == (
      False,
      ['last session exited with code 3'],
    )

  def test_not_clean_after_a_killed_session(self, tmp_path):
    model.record_session_end(tmp_path, 'ws', None)
    assert HostWorktree('ws', tmp_path).is_clean() == (False, ['last session was killed'])

  def test_container_record_keyed_by_the_prefixed_ref(self, tmp_path):
    workspace = ContainerWorkspace('ws', tmp_path)
    model.record_session_end(tmp_path, workspace.ref, 0)
    assert workspace.is_clean() == (True, [])
    # the same-name host worktree keeps its own record
    assert HostWorktree('ws', tmp_path).is_clean()[0] is False


class TestSessionLock:
  def test_idle_workspace_is_inactive(self, tmp_path):
    assert HostWorktree('feat', tmp_path).is_active(set()) is False

  def test_held_lock_reads_as_active(self, tmp_path):
    workspace = HostWorktree('feat', tmp_path)
    with workspace.hold_session_lock():
      assert workspace.is_active(set()) is True
      assert workspace.lockfile.read_text() == str(os.getpid())
    assert workspace.is_active(set()) is False

  def test_a_second_holder_is_refused(self, tmp_path):
    workspace = HostWorktree('feat', tmp_path)
    with workspace.hold_session_lock():
      with pytest.raises(model.SessionBusy, match=f'feat.*pid {os.getpid()}'):
        with HostWorktree('feat', tmp_path).hold_session_lock():
          pass

  def test_the_lock_dies_with_its_holder(self, tmp_path):
    # flock releases on process death, so a killed session leaves nothing stale
    workspace = HostWorktree('feat', tmp_path)
    workspace.lockfile.parent.mkdir(parents=True, exist_ok=True)
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

  def test_a_host_worktree_and_its_same_name_container_lock_apart(self, tmp_path):
    with HostWorktree('feat', tmp_path).hold_session_lock():
      with ContainerWorkspace('feat', tmp_path).hold_session_lock():
        pass

  def test_container_active_when_path_in_mounts(self, tmp_path, monkeypatch):
    # a running container counts even with no launcher holding the lock
    monkeypatch.setattr(model, 'containers_dir', lambda project: tmp_path)
    workspace = ContainerWorkspace('feat', tmp_path)
    assert workspace.is_active({str(workspace.path)}) is True
    assert workspace.is_active(set()) is False


class TestWorkspaceRefsAndEnumeration:
  def test_from_ref_resolves_host_and_container(self, tmp_path, monkeypatch):
    worktrees = tmp_path / 'wt'
    containers = tmp_path / 'ct'
    (worktrees / 'h').mkdir(parents=True)
    (containers / 'c').mkdir(parents=True)
    monkeypatch.setattr(model, 'worktrees_dir', lambda project: worktrees)
    monkeypatch.setattr(model, 'containers_dir', lambda project: containers)
    host = Workspace.from_ref('h', tmp_path)
    container = Workspace.from_ref('c:c', tmp_path)
    assert isinstance(host, HostWorktree) and host.ref == 'h'
    assert isinstance(container, ContainerWorkspace) and container.ref == 'c:c'

  def test_from_ref_raises_with_kind_specific_message(self, tmp_path, monkeypatch):
    monkeypatch.setattr(model, 'worktrees_dir', lambda project: tmp_path / 'wt')
    monkeypatch.setattr(model, 'containers_dir', lambda project: tmp_path / 'ct')
    with pytest.raises(ValueError, match='^workspace not found: gone$'):
      Workspace.from_ref('gone', tmp_path)
    with pytest.raises(ValueError, match='^container workspace not found: c:gone$'):
      Workspace.from_ref('c:gone', tmp_path)

  def test_all_enumerates_both_dirs(self, tmp_path, monkeypatch):
    worktrees = tmp_path / 'wt'
    containers = tmp_path / 'ct'
    (worktrees / 'h1').mkdir(parents=True)
    (worktrees / 'h2').mkdir(parents=True)
    (containers / 'c1').mkdir(parents=True)
    monkeypatch.setattr(model, 'worktrees_dir', lambda project: worktrees)
    monkeypatch.setattr(model, 'containers_dir', lambda project: containers)
    refs = {workspace.ref for workspace in Workspace.all(tmp_path)}
    assert refs == {'h1', 'h2', 'c:c1'}
