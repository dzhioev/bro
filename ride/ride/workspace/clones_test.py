import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ride.repository import Repository
from ride.workspace.clones import ensure_container_clone


def _git(*arguments: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ['git', *arguments],
    cwd=cwd,
    check=check,
    capture_output=True,
    text=True,
    env={
      **os.environ,
      'GIT_AUTHOR_NAME': 'Test',
      'GIT_AUTHOR_EMAIL': 'test@example.com',
      'GIT_COMMITTER_NAME': 'Test',
      'GIT_COMMITTER_EMAIL': 'test@example.com',
    },
  )


def _initialize_repository(path: Path) -> None:
  path.mkdir()
  _git('init', '--quiet', '--initial-branch', 'master', cwd=path)


def _commit(path: Path, filename: str) -> str:
  (path / filename).write_text(filename)
  _git('add', filename, cwd=path)
  _git('commit', '--quiet', '--message', filename, cwd=path)
  return _git('rev-parse', 'HEAD', cwd=path).stdout.strip()


def _source_repository(
  tmp_path: Path, *, origin: str = 'https://example.test/repository.git'
) -> Path:
  source = tmp_path / 'source'
  _initialize_repository(source)
  _commit(source, 'base')
  _git('remote', 'add', 'origin', origin, cwd=source)
  return source


def _repository(source: Path) -> Repository:
  return Repository(str(source), source)


def _alternates(repository: Path) -> Path:
  path = Path(
    _git('rev-parse', '--git-path', 'objects/info/alternates', cwd=repository).stdout.strip()
  )
  return path if path.is_absolute() else repository / path


def test_clone_has_its_own_objects_upstream_and_workspace_branch(tmp_path):
  source = _source_repository(tmp_path, origin='git@github.com:owner/repository.git')
  head = _git('rev-parse', 'HEAD', cwd=source).stdout.strip()
  _git('update-ref', 'refs/remotes/origin/fresh', head, cwd=source)
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(_repository(source), tree, 'worktree-session')

  assert _git('symbolic-ref', '--short', 'HEAD', cwd=tree).stdout.strip() == 'worktree-session'
  assert _git('rev-parse', 'HEAD', cwd=tree).stdout.strip() == head
  assert _git('rev-parse', 'refs/remotes/origin/fresh', cwd=tree).stdout.strip() == head
  assert _git('remote', 'get-url', 'origin', cwd=tree).stdout.strip() == (
    'https://github.com/owner/repository.git'
  )
  assert _git('remote', cwd=tree).stdout.split() == ['origin']
  assert not _alternates(tree).exists()

  shutil.rmtree(source)
  assert _git('cat-file', '-e', f'{head}^{{commit}}', cwd=tree).returncode == 0


def test_explicit_base_is_checked_out_only_when_the_clone_is_created(tmp_path):
  source = _source_repository(tmp_path)
  base = _git('rev-parse', 'HEAD', cwd=source).stdout.strip()
  _commit(source, 'later')
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(_repository(source), tree, 'worktree-session', base)
  (tree / 'local').write_text('preserve')
  ensure_container_clone(
    _repository(source),
    tree,
    'worktree-session',
    _git('rev-parse', 'HEAD', cwd=source).stdout.strip(),
  )

  assert _git('rev-parse', 'HEAD', cwd=tree).stdout.strip() == base
  assert (tree / 'local').read_text() == 'preserve'


def test_clone_from_an_alternates_source_is_dissociated(tmp_path):
  upstream = _source_repository(tmp_path)
  source = tmp_path / 'shared-source'
  _git('clone', '--quiet', '--shared', str(upstream), str(source), cwd=tmp_path)
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(_repository(source), tree, 'worktree-session')

  assert not _alternates(tree).exists()
  shutil.rmtree(upstream)
  shutil.rmtree(source)
  assert _git('fsck', '--full', cwd=tree).returncode == 0


def test_legacy_shared_clone_is_refused(tmp_path):
  source = _source_repository(tmp_path)
  tree = tmp_path / 'legacy' / 'tree'
  _git('clone', '--quiet', '--shared', str(source), str(tree), cwd=tmp_path)

  with pytest.raises(RuntimeError, match=r'ride clean --force legacy'):
    ensure_container_clone(_repository(source), tree, 'worktree-legacy')


def test_failed_preparation_leaves_no_partial_clone(tmp_path):
  source = _source_repository(tmp_path)
  _git('remote', 'remove', 'origin', cwd=source)
  tree = tmp_path / 'workspace' / 'tree'
  tree.mkdir(parents=True)

  with pytest.raises(RuntimeError, match='remote get-url origin'):
    ensure_container_clone(_repository(source), tree, 'worktree-session')

  assert tree.is_dir()
  assert list(tree.iterdir()) == []


def _superproject_with_submodule(tmp_path: Path) -> tuple[Path, Path]:
  submodule = tmp_path / 'submodule-upstream'
  _initialize_repository(submodule)
  _commit(submodule, 'component')
  superproject = tmp_path / 'superproject-upstream'
  _initialize_repository(superproject)
  _git(
    '-c',
    'protocol.file.allow=always',
    'submodule',
    'add',
    '--quiet',
    str(submodule),
    'component',
    cwd=superproject,
  )
  _git('commit', '--quiet', '--message', 'submodule', cwd=superproject)
  return superproject, submodule


def test_initialized_checkout_submodule_is_cloned_locally_and_retargeted(tmp_path):
  upstream, submodule_upstream = _superproject_with_submodule(tmp_path)
  source = tmp_path / 'source'
  _git('clone', '--quiet', str(upstream), str(source), cwd=tmp_path)
  _git('-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', cwd=source)
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(_repository(source), tree, 'worktree-session')

  component = tree / 'component'
  assert (component / 'component').read_text() == 'component'
  assert _git('remote', 'get-url', 'origin', cwd=component).stdout.strip() == str(
    submodule_upstream
  )
  assert not _alternates(component).exists()
  assert _git('remote', cwd=component).stdout.split() == ['origin']


def test_uninitialized_checkout_submodule_is_skipped(tmp_path):
  upstream, _ = _superproject_with_submodule(tmp_path)
  source = tmp_path / 'source'
  _git('clone', '--quiet', str(upstream), str(source), cwd=tmp_path)
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(_repository(source), tree, 'worktree-session')

  assert not (tree / 'component' / 'component').exists()


def test_bare_mirror_submodule_uses_the_committed_url(tmp_path):
  upstream, submodule_upstream = _superproject_with_submodule(tmp_path)
  mirror = tmp_path / 'mirror.git'
  _git('clone', '--quiet', '--bare', str(upstream), str(mirror), cwd=tmp_path)
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(
    Repository('https://example.test/repository.git', mirror), tree, 'worktree-session'
  )

  component = tree / 'component'
  assert (component / 'component').read_text() == 'component'
  assert _git('remote', 'get-url', 'origin', cwd=component).stdout.strip() == str(
    submodule_upstream
  )
  assert not _alternates(component).exists()


def test_bare_mirror_resolves_a_relative_submodule_url_against_origin(tmp_path):
  superproject, submodule = _superproject_with_submodule(tmp_path)
  remotes = tmp_path / 'remotes'
  remotes.mkdir()
  component_remote = remotes / 'component.git'
  _git('clone', '--quiet', '--bare', str(submodule), str(component_remote), cwd=tmp_path)
  modules = superproject / '.gitmodules'
  modules.write_text(modules.read_text().replace(str(submodule), '../component.git'))
  _git('add', '.gitmodules', cwd=superproject)
  _git('commit', '--quiet', '--message', 'relative submodule', cwd=superproject)
  superproject_remote = remotes / 'superproject.git'
  _git('clone', '--quiet', '--bare', str(superproject), str(superproject_remote), cwd=tmp_path)

  mirror = tmp_path / 'mirror.git'
  mirror.mkdir()
  _git('init', '--quiet', '--bare', cwd=mirror)
  _git('remote', 'add', 'origin', str(superproject_remote), cwd=mirror)
  _git(
    'config',
    'remote.origin.fetch',
    '+refs/heads/*:refs/remotes/origin/*',
    cwd=mirror,
  )
  _git('fetch', '--quiet', 'origin', cwd=mirror)
  _git('symbolic-ref', 'HEAD', 'refs/remotes/origin/master', cwd=mirror)
  tree = tmp_path / 'workspace' / 'tree'

  ensure_container_clone(
    Repository('https://example.test/repository.git', mirror), tree, 'worktree-session'
  )

  component = tree / 'component'
  assert (component / 'component').read_text() == 'component'
  assert _git('remote', 'get-url', 'origin', cwd=component).stdout.strip() == str(component_remote)
