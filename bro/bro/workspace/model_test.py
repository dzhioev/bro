import os
import subprocess

import pytest

from workspace import model
from workspace.model import ContainerWorkspace, HostWorktree, Workspace


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


def _git(cwd, *args):
  subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(d):
  d.mkdir(parents=True, exist_ok=True)
  _git(d, 'init', '-b', 'master')
  _git(d, 'config', 'user.email', 't@t')
  _git(d, 'config', 'user.name', 't')
  (d / 'f').write_text('a')
  _git(d, 'add', '.')
  _git(d, 'commit', '-m', 'c1')


class TestCleanupImage:
  def test_prefers_current_tag_when_present(self, monkeypatch):
    monkeypatch.setattr(model, 'image_tag', lambda: 'bro/ppp-dev:cur')
    monkeypatch.setattr(model.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    assert model._cleanup_image() == 'bro/ppp-dev:cur'

  def test_falls_back_to_any_ppp_cw_image(self, monkeypatch):
    monkeypatch.setattr(model, 'image_tag', lambda: 'bro/ppp-dev:cur')

    def fake_run(argv, *a, **k):
      if argv[1] == 'image':  # docker image inspect -> miss
        return _FakeProc(returncode=1)
      return _FakeProc(returncode=0, stdout='bro/ppp-dev:<none>\nbro/ppp-dev:abc123\n')

    monkeypatch.setattr(model.subprocess, 'run', fake_run)
    assert model._cleanup_image() == 'bro/ppp-dev:abc123'

  def test_none_when_no_image(self, monkeypatch):
    monkeypatch.setattr(model, 'image_tag', lambda: 'bro/ppp-dev:cur')

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
    model._remove_container_dir(tmp_path / 'ws', image='bro/ppp-dev:x')
    assert calls == [tmp_path / 'ws']

  def test_missing_dir_is_noop(self, monkeypatch, tmp_path):
    def boom(_):
      raise FileNotFoundError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    monkeypatch.setattr(
      model.subprocess, 'run', lambda *a, **k: pytest.fail('docker must not be invoked')
    )
    model._remove_container_dir(tmp_path / 'gone', image='bro/ppp-dev:x')

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
    model._remove_container_dir(target, image='bro/ppp-dev:x')
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
      model._remove_container_dir(tmp_path / 'ws', image='bro/ppp-dev:x')

  def test_raises_when_dir_survives_docker_rm(self, monkeypatch, tmp_path):
    def boom(_):
      raise PermissionError

    monkeypatch.setattr(model.shutil, 'rmtree', boom)
    monkeypatch.setattr(model.subprocess, 'run', lambda *a, **k: _FakeProc(returncode=0))
    survivor = tmp_path / 'ws'
    survivor.mkdir()  # still present after the mocked docker rm
    with pytest.raises(RuntimeError, match='still present'):
      model._remove_container_dir(survivor, image='bro/ppp-dev:x')


class TestContainerWorkspaceRemove:
  def test_removes_dir_with_cleanup_image_and_host_log(self, monkeypatch, tmp_path):
    monkeypatch.setattr(model, 'containers_dir', lambda project: tmp_path / 'containers')
    monkeypatch.setattr(model, '_cleanup_image', lambda: 'bro/ppp-dev:img')
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
    workspace.remove()
    assert removed == {'path': workspace.path, 'image': 'bro/ppp-dev:img'}
    assert not host_log.exists()  # the session host log goes with the workspace

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
    workspace.remove()
    assert calls == [
      ('worktree', 'remove', '--force', str(workspace.path)),
      ('branch', '-D', 'worktree-ws'),
    ]
    assert not host_log.exists()


class TestHostIsClean:
  def _make_workspace(self, tmp_path, monkeypatch, name='repo'):
    monkeypatch.setattr(model, 'worktrees_dir', lambda project: tmp_path)
    workspace = HostWorktree(name, tmp_path)
    _init_repo(workspace.path)
    _git(workspace.path, 'update-ref', 'refs/remotes/origin/master', 'HEAD')
    return workspace

  def test_clean_when_head_matches_origin_master(self, tmp_path, monkeypatch):
    workspace = self._make_workspace(tmp_path, monkeypatch)
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is True
    assert reasons == []

  def test_counts_unpushed_commits(self, tmp_path, monkeypatch):
    workspace = self._make_workspace(tmp_path, monkeypatch)
    (workspace.path / 'f').write_text('b')
    _git(workspace.path, 'commit', '-am', 'c2')
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is False
    assert reasons == ['1 commit(s) not on origin/master']

  def test_flags_uncommitted_changes(self, tmp_path, monkeypatch):
    workspace = self._make_workspace(tmp_path, monkeypatch)
    (workspace.path / 'untracked').write_text('x')
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is False
    assert 'uncommitted or untracked changes' in reasons

  def test_missing_origin_master_is_not_clean(self, tmp_path, monkeypatch):
    monkeypatch.setattr(model, 'worktrees_dir', lambda project: tmp_path)
    workspace = HostWorktree('repo', tmp_path)
    _init_repo(workspace.path)  # no origin/master ref set
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is False
    assert 'origin/master not found' in reasons


class TestContainerIsClean:
  def _make_workspace(self, tmp_path, monkeypatch):
    # a shared "host" repo (the ancestry check's check_root) and a clone of it
    # standing in for the container workspace, exercising the alternate-objects
    # dance: the clone's local commits are reachable from the shared repo only
    # via GIT_ALTERNATE_OBJECT_DIRECTORIES.
    project = tmp_path / 'project'
    _init_repo(project)
    _git(project, 'update-ref', 'refs/remotes/origin/master', 'HEAD')
    containers = tmp_path / 'containers'
    containers.mkdir()
    monkeypatch.setattr(model, 'containers_dir', lambda p: containers)
    workspace = ContainerWorkspace('ws', project)
    _git(tmp_path, 'clone', '--quiet', str(project), str(workspace.path))
    _git(workspace.path, 'config', 'user.email', 't@t')
    _git(workspace.path, 'config', 'user.name', 't')
    return workspace

  def test_clean_when_clone_head_matches_origin_master(self, tmp_path, monkeypatch):
    workspace = self._make_workspace(tmp_path, monkeypatch)
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is True
    assert reasons == []

  def test_counts_unpushed_clone_commits_via_alternate(self, tmp_path, monkeypatch):
    workspace = self._make_workspace(tmp_path, monkeypatch)
    (workspace.path / 'f').write_text('b')
    _git(workspace.path, 'commit', '-am', 'c2')
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is False
    assert reasons == ['1 commit(s) not on origin/master']

  def test_not_a_git_repository(self, tmp_path, monkeypatch):
    containers = tmp_path / 'containers'
    (containers / 'ws').mkdir(parents=True)  # no .git
    monkeypatch.setattr(model, 'containers_dir', lambda p: containers)
    workspace = ContainerWorkspace('ws', tmp_path / 'project')
    safe, reasons = workspace.is_clean(refresh_origin=False)
    assert safe is False
    assert reasons == ['not a git repository']


class TestIsActive:
  def _make_workspace(self, tmp_path, name='feat'):
    return HostWorktree(name, tmp_path)

  def _seed(self, workspace):
    workspace.pidfile.parent.mkdir(parents=True, exist_ok=True)
    return workspace.pidfile

  def test_host_false_when_no_pidfile(self, tmp_path):
    assert self._make_workspace(tmp_path).is_active(set()) is False

  def test_host_true_for_live_pid(self, tmp_path):
    workspace = self._make_workspace(tmp_path)
    self._seed(workspace).write_text(str(os.getpid()))
    assert workspace.is_active(set()) is True

  def test_host_false_for_dead_pid(self, tmp_path):
    process = subprocess.Popen(['true'])
    process.wait()
    workspace = self._make_workspace(tmp_path)
    self._seed(workspace).write_text(str(process.pid))
    assert workspace.is_active(set()) is False

  def test_host_false_for_garbage(self, tmp_path):
    workspace = self._make_workspace(tmp_path)
    self._seed(workspace).write_text('not-a-pid')
    assert workspace.is_active(set()) is False

  def test_host_ignores_mounts(self, tmp_path):
    workspace = self._make_workspace(tmp_path)
    self._seed(workspace).write_text(str(os.getpid()))
    assert workspace.is_active({str(workspace.path)}) is True  # mounts are irrelevant to a worktree

  def test_container_active_when_path_in_mounts(self, tmp_path, monkeypatch):
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
