"""local checkout and managed git-URL repository attachments."""

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from bro.workspace.git import git_run, rev_parse_commit
from bro.workspace.paths import project_root, runtime_base
from bro.workspace.project import ProjectConfig, project_config, project_config_from_text

_SCHEME_URL = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*://')
_SCP_URL = re.compile(r'^(?:[^/@:\s]+@)?[^/:\s]+:.+$')
_SLUG_CHARACTER = re.compile(r'[^a-zA-Z0-9]+')


@dataclass(frozen=True)
class Repository:
  """an attachment identity and the host git repository that supplies it."""

  identity: str
  git_dir: Path
  tree_ref: Optional[str] = None

  @property
  def is_url(self) -> bool:
    return self.tree_ref is not None

  @property
  def credential_root(self) -> Optional[Path]:
    return None if self.is_url else self.git_dir

  @property
  def default_base(self) -> Optional[str]:
    return self.tree_ref

  def read_file(self, relative: str) -> Optional[bytes]:
    _validate_relative_path(relative)
    if not self.is_url:
      path = self.git_dir / relative
      return path.read_bytes() if path.is_file() else None
    assert self.tree_ref is not None
    result = subprocess.run(
      ['git', 'show', f'{self.tree_ref}:{relative}'], cwd=self.git_dir, capture_output=True
    )
    if result.returncode == 0:
      return result.stdout
    missing = subprocess.run(
      ['git', 'cat-file', '-e', f'{self.tree_ref}:{relative}'],
      cwd=self.git_dir,
      capture_output=True,
    )
    if missing.returncode != 0:
      return None
    raise RuntimeError(
      f'cannot read {relative} from {self.identity}: {result.stderr.decode().strip()}'
    )

  def list_files(self) -> list[str]:
    if self.is_url:
      assert self.tree_ref is not None
      command = ['git', 'ls-tree', '-r', '-z', '--name-only', self.tree_ref]
    else:
      command = ['git', 'ls-files', '-z']
    result = subprocess.run(command, cwd=self.git_dir, capture_output=True)
    if result.returncode != 0:
      raise RuntimeError(f'cannot list files in {self.identity}: {result.stderr.decode().strip()}')
    return [name for name in result.stdout.decode().split('\0') if name]

  def project_config(self) -> ProjectConfig:
    if not self.is_url:
      return project_config(self.git_dir)
    content = self.read_file('pyproject.toml')
    if content is None:
      raise ValueError(f'missing pyproject.toml in {self.identity} at {self.tree_ref}')
    return project_config_from_text(
      content.decode(), f'{self.identity}:{self.tree_ref}:pyproject.toml'
    )


def _validate_relative_path(relative: str) -> None:
  path = PurePosixPath(relative)
  if path.is_absolute() or '..' in path.parts:
    raise ValueError(f'repository path must be relative: {relative!r}')


def is_git_url(value: str) -> bool:
  return _SCHEME_URL.match(value) is not None or _SCP_URL.match(value) is not None


def normalize_git_url(value: str) -> str:
  if _SCHEME_URL.match(value) is not None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname is None:
      if parsed.scheme.lower() != 'file' or parsed.netloc:
        raise ValueError(f'malformed git URL: {value!r}')
      netloc = ''
    else:
      hostname = parsed.hostname.lower()
      if ':' in hostname and not hostname.startswith('['):
        hostname = f'[{hostname}]'
      user_info = parsed.netloc.rsplit('@', 1)[0] + '@' if '@' in parsed.netloc else ''
      port = '' if parsed.port is None else f':{parsed.port}'
      netloc = f'{user_info}{hostname}{port}'
    path = parsed.path.rstrip('/') or '/'
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ''))
  if _SCP_URL.match(value) is not None:
    host, path = value.split(':', 1)
    if '@' in host:
      user, hostname = host.rsplit('@', 1)
      host = f'{user}@{hostname.lower()}'
    else:
      host = host.lower()
    return f'{host}:{path.rstrip("/")}'
  raise ValueError(f'not a git URL: {value!r}')


def mirror_key(url: str) -> str:
  normalized = normalize_git_url(url)
  parsed_path = (
    urllib.parse.urlsplit(normalized).path
    if _SCHEME_URL.match(normalized)
    else normalized.split(':', 1)[-1]
  )
  slug = _SLUG_CHARACTER.sub('-', parsed_path.strip('/')).strip('-').lower() or 'repository'
  slug = slug.removesuffix('-git')[-48:]
  digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
  return f'{slug}-{digest}'


def repositories_dir() -> Path:
  return runtime_base() / 'repos'


def mirror_path(url: str) -> Path:
  return repositories_dir() / mirror_key(url)


@contextlib.contextmanager
def _mirror_lock(key: str):
  lock_dir = repositories_dir() / '.locks'
  lock_dir.mkdir(parents=True, exist_ok=True)
  handle = os.fdopen(os.open(lock_dir / key, os.O_RDWR | os.O_CREAT, 0o600), 'r+')
  with contextlib.closing(handle):
    fcntl.flock(handle, fcntl.LOCK_EX)
    yield


def _run_checked(root: Path, *args: str) -> None:
  result = git_run(*args, cwd=root)
  if result.returncode != 0:
    raise RuntimeError(f'git {" ".join(args)} failed for {root}: {result.stderr.strip()}')


def _create_mirror(url: str, target: Path) -> None:
  target.parent.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(prefix=f'.{target.name}-', dir=target.parent) as directory:
    staging = Path(directory)
    _run_checked(staging, 'init', '--bare', '--quiet')
    _run_checked(staging, 'remote', 'add', 'origin', url)
    _run_checked(staging, 'config', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
    _run_checked(staging, 'config', 'gc.auto', '0')
    _run_checked(staging, 'config', 'fetch.prune', 'false')
    _run_checked(staging, 'config', 'fetch.pruneTags', 'false')
    _run_checked(staging, 'config', 'remote.origin.prune', 'false')
    _run_checked(staging, 'fetch', '--no-prune', '--no-prune-tags', 'origin')
    _run_checked(staging, 'remote', 'set-head', 'origin', '--auto')
    staging.rename(target)


def _align_mirror_head(target: Path) -> None:
  result = git_run('symbolic-ref', 'refs/remotes/origin/HEAD', cwd=target)
  if result.returncode != 0:
    raise RuntimeError(f'cannot resolve origin/HEAD for {target}: {result.stderr.strip()}')
  _run_checked(target, 'symbolic-ref', 'HEAD', result.stdout.strip())


def _refresh_mirror(normalized: str) -> Repository:
  target = mirror_path(normalized)
  if not target.exists():
    _create_mirror(normalized, target)
  elif not (target / 'HEAD').is_file():
    raise RuntimeError(f'managed mirror is not a bare git repository: {target}')
  _run_checked(target, 'config', 'gc.auto', '0')
  _run_checked(target, 'config', 'fetch.prune', 'false')
  _run_checked(target, 'config', 'fetch.pruneTags', 'false')
  _run_checked(target, 'config', 'remote.origin.prune', 'false')
  _run_checked(target, 'fetch', '--no-prune', '--no-prune-tags', 'origin')
  _run_checked(target, 'remote', 'set-head', 'origin', '--auto')
  _align_mirror_head(target)
  base = rev_parse_commit(target, 'refs/remotes/origin/HEAD')
  if base is None:
    raise RuntimeError(f'{normalized} has no resolvable origin/HEAD')
  return Repository(normalized, target, base)


def _fetch_mirror(url: str) -> Repository:
  normalized = normalize_git_url(url)
  with _mirror_lock(mirror_key(normalized)):
    return _refresh_mirror(normalized)


@contextlib.contextmanager
def hold_repository(identity: str) -> Generator[Repository]:
  """refresh and hold a URL mirror against cleanup until its workspace is recorded."""
  if not is_git_url(identity):
    yield open_repository(identity)
    return
  normalized = normalize_git_url(identity)
  with _mirror_lock(mirror_key(normalized)):
    yield _refresh_mirror(normalized)


def open_repository(identity: str) -> Repository:
  """open a recorded attachment without network access."""
  if is_git_url(identity):
    normalized = normalize_git_url(identity)
    target = mirror_path(normalized)
    base = rev_parse_commit(target, 'refs/remotes/origin/HEAD') if target.is_dir() else None
    if base is None:
      raise RuntimeError(f'managed mirror is missing or unreadable for {normalized}')
    return Repository(normalized, target, base)
  root = Path(identity)
  if not root.is_dir():
    raise RuntimeError(f'attached repository no longer exists: {root}')
  return Repository(str(root), root)


def clean_managed_mirrors(referenced: set[str], *, dry_run: bool = False) -> tuple[int, int]:
  root = repositories_dir()
  if not root.is_dir():
    return 0, 0
  referenced_paths = {mirror_path(identity) for identity in referenced if is_git_url(identity)}
  removed = 0
  skipped = 0
  for path in sorted(root.iterdir()):
    if not path.is_dir() or path.name == '.locks':
      continue
    if path in referenced_paths:
      skipped += 1
      continue
    lock_path = root / '.locks' / path.name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), 'r+')
    with contextlib.closing(handle):
      try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        skipped += 1
        continue
      if not dry_run:
        shutil.rmtree(path)
        lock_path.unlink(missing_ok=True)
      removed += 1
  return removed, skipped


def as_repository(value: Repository | Path) -> Repository:
  if isinstance(value, Repository):
    return value
  root = value.resolve()
  return Repository(str(root), root)


def resolve_repository(value: str) -> Repository:
  """resolve an existing checkout path or fetch a URL-shaped managed mirror."""
  path = Path(value).expanduser()
  if path.exists():
    root = project_root(path)
    return Repository(str(root), root)
  if is_git_url(value):
    return _fetch_mirror(value)
  raise ValueError(f'--repo must name an existing checkout path or a git URL, not {value!r}')
