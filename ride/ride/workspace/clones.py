import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.workspace.git import no_prompt_env
from ride.repository import Repository


def _git(root: Path, *arguments: str) -> str:
  result = subprocess.run(
    ['git', *arguments],
    cwd=root,
    capture_output=True,
    text=True,
    env=no_prompt_env(),
  )
  if result.returncode != 0:
    raise RuntimeError(f'git {" ".join(arguments)} failed for {root}: {result.stderr.strip()}')
  return result.stdout.strip()


def _container_git_url(url: str) -> str:
  prefix = 'git@github.com:'
  if url.startswith(prefix):
    return f'https://github.com/{url.removeprefix(prefix)}'
  return url


def _origin_url(repository: Path) -> str:
  return _container_git_url(_git(repository, 'remote', 'get-url', 'origin'))


def _alternates_file(repository: Path) -> Path:
  git_directory = Path(_git(repository, 'rev-parse', '--git-dir'))
  if not git_directory.is_absolute():
    git_directory = repository / git_directory
  return git_directory / 'objects' / 'info' / 'alternates'


def _detach_alternates(repository: Path) -> None:
  alternates = _alternates_file(repository)
  if not alternates.is_file():
    return
  _git(repository, 'repack', '-a', '-d')
  alternates.unlink()


def _submodules(repository: Path) -> list[tuple[str, Path]]:
  modules = repository / '.gitmodules'
  if not modules.is_file():
    return []
  output = _git(
    repository,
    'config',
    '--file',
    '.gitmodules',
    '--get-regexp',
    r'^submodule\..*\.path$',
  )
  entries = []
  for line in output.splitlines():
    key, relative = line.split(maxsplit=1)
    name = key.removeprefix('submodule.').removesuffix('.path')
    entries.append((name, Path(relative)))
  return entries


def _initialize_submodules(repository: Repository, tree: Path) -> None:
  source = repository.git_dir
  is_bare = _git(source, 'rev-parse', '--is-bare-repository') == 'true'
  quiet = [] if log.verbose_enabled() else ['--quiet']
  for name, relative in _submodules(tree):
    local_source = source / relative
    if (local_source / '.git').exists():
      upstream = _origin_url(local_source)
      clone_source = str(local_source)
    elif is_bare:
      _git(tree, 'submodule', 'init', '--', str(relative))
      upstream = _container_git_url(_git(tree, 'config', '--get', f'submodule.{name}.url'))
      clone_source = upstream
    else:
      log.verbose('skipping submodule %s: %s is not initialized', name, local_source)
      continue
    log.verbose('initializing submodule %s from %s', name, clone_source)
    _git(tree, 'config', f'submodule.{name}.url', upstream)
    _git(
      tree,
      '-c',
      f'submodule.{name}.url={clone_source}',
      '-c',
      'protocol.file.allow=always',
      'submodule',
      'update',
      '--init',
      *quiet,
      '--',
      str(relative),
    )
    submodule = tree / relative
    _git(submodule, 'remote', 'set-url', 'origin', upstream)
    _detach_alternates(submodule)


def _prepare_clone(
  repository: Repository, tree: Path, branch: str, base_ref: Optional[str]
) -> None:
  quiet = [] if log.verbose_enabled() else ['--quiet']
  result = subprocess.run(
    [
      'git',
      '-c',
      'protocol.file.allow=always',
      'clone',
      *quiet,
      str(repository.git_dir),
      str(tree),
    ],
    capture_output=True,
    text=True,
    env=no_prompt_env(),
  )
  if result.returncode != 0:
    raise RuntimeError(f'git clone {repository.git_dir} failed for {tree}: {result.stderr.strip()}')
  _git(tree, 'remote', 'set-url', 'origin', _origin_url(repository.git_dir))
  _git(
    tree,
    'fetch',
    str(repository.git_dir),
    '+refs/remotes/origin/*:refs/remotes/origin/*',
  )
  _git(tree, 'checkout', *quiet, '-B', branch, base_ref if base_ref is not None else 'HEAD')
  _initialize_submodules(repository, tree)
  _detach_alternates(tree)


def ensure_container_clone(
  repository: Repository, tree: Path, branch: str, base_ref: Optional[str] = None
) -> None:
  """create a container workspace's host-side clone on its first launch."""
  git_directory = tree / '.git'
  if git_directory.exists():
    if (git_directory / 'objects' / 'info' / 'alternates').is_file():
      raise RuntimeError(
        f'workspace {tree.parent.name!r} uses a legacy shared clone; '
        f'recreate it with `ride clean --force {tree.parent.name}`'
      )
    return
  if tree.exists() and any(tree.iterdir()):
    raise RuntimeError(f'container workspace tree is not empty and has no git clone: {tree}')
  tree.parent.mkdir(parents=True, exist_ok=True)
  log.info('creating container clone %s', tree)
  with tempfile.TemporaryDirectory(prefix=f'.{tree.name}-', dir=tree.parent) as directory:
    prepared = Path(directory) / 'clone'
    _prepare_clone(repository, prepared, branch, base_ref)
    if tree.exists():
      tree.rmdir()
    prepared.rename(tree)
