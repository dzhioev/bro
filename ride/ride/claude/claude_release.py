"""Claude Code's pinned standalone release and host cache."""

import base64
import contextlib
import fcntl
import hashlib
import json
import platform
import shutil
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from bro.base import log
from bro.workspace.paths import runtime_base

RELEASES_URL = 'https://downloads.claude.ai/claude-code-releases'
_CACHE_DIRECTORY = 'claude-code'
_LOCK_DIRECTORY = 'claude-code-locks'
_RESERVATIONS_GUARD = threading.Lock()
_RESERVATIONS: dict[Path, BinaryIO] = {}
_RESERVATION_LOCKS: dict[Path, threading.Lock] = {}


def host_platform() -> str:
  """the release manifest's name for the platform this process runs on."""
  systems = {'linux': 'linux', 'darwin': 'darwin'}
  machines = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}
  machine = platform.machine().lower()
  if sys.platform not in systems or machine not in machines:
    raise ValueError(f'Claude Code publishes no release for {sys.platform}/{machine}')
  if sys.platform == 'linux' and platform.libc_ver()[0] != 'glibc':
    raise ValueError('Claude Code release selection supports only glibc linux hosts')
  return f'{systems[sys.platform]}-{machines[machine]}'


def _digest_valid(value: object) -> bool:
  return (
    isinstance(value, str)
    and len(value) == 64
    and all(character in '0123456789abcdef' for character in value)
  )


def _file_digest(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  return digest.hexdigest()


def _fetch_json(url: str) -> dict[str, object]:
  with urllib.request.urlopen(url) as response:
    manifest = json.load(response)
  if not isinstance(manifest, dict):
    raise ValueError(f'{url} is not a JSON object')
  return manifest


def _download(url: str, into: Path) -> None:
  with urllib.request.urlopen(url) as response, into.open('wb') as file:
    shutil.copyfileobj(response, file)


def checksum(release_manifest: dict[str, object], platform_name: str) -> str:
  """the digest the release manifest states for `platform_name`."""
  platforms = release_manifest.get('platforms')
  if not isinstance(platforms, dict) or platform_name not in platforms:
    raise ValueError(f'the Claude Code release manifest has no {platform_name} platform')
  entry = platforms[platform_name]
  stated = entry.get('checksum') if isinstance(entry, dict) else None
  if not _digest_valid(stated):
    raise ValueError(f'the Claude Code release manifest has no {platform_name} checksum')
  return str(stated)


def _component(value: str, label: str) -> str:
  if (
    value == ''
    or not value[0].isalnum()
    or any(not (character.isalnum() or character in '.-_') for character in value)
  ):
    raise ValueError(f'Claude Code {label} is not a cache path component: {value!r}')
  return value


def cache_root() -> Path:
  """the runtime-wide cache of pinned Claude Code releases."""
  return runtime_base() / _CACHE_DIRECTORY


def _version_directory(version: str) -> Path:
  return cache_root() / _component(version, 'version')


def _binary(version: str, platform_name: str) -> Path:
  return _version_directory(version) / _component(platform_name, 'platform') / 'claude'


def _checksum_record(binary: Path) -> Path:
  return binary.with_suffix('.sha256')


def _lock_directory(version: str) -> Path:
  encoded = base64.urlsafe_b64encode(_component(version, 'version').encode()).decode().rstrip('=')
  return runtime_base() / _LOCK_DIRECTORY / encoded


def _version_lock_path(version: str) -> Path:
  return _lock_directory(version) / 'version.lock'


def _download_lock_path(version: str) -> Path:
  return _lock_directory(version) / 'download.lock'


def _reservation_lock(path: Path) -> threading.Lock:
  with _RESERVATIONS_GUARD:
    return _RESERVATION_LOCKS.setdefault(path, threading.Lock())


def _reserve_version(version: str) -> None:
  """hold a version's shared lock for this process's remaining lifetime."""
  path = _version_lock_path(version)
  with _reservation_lock(path):
    with _RESERVATIONS_GUARD:
      if path in _RESERVATIONS:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as cleanup:
      handle = cleanup.enter_context(path.open('a+b'))
      fcntl.flock(handle, fcntl.LOCK_SH)
      with _RESERVATIONS_GUARD:
        _RESERVATIONS[path] = handle
      cleanup.pop_all()


@contextlib.contextmanager
def _download_lock(version: str) -> Iterator[None]:
  path = _download_lock_path(version)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('a+b') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    yield


@contextlib.contextmanager
def _removal_lock(version: str) -> Iterator[bool]:
  path = _version_lock_path(version)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('a+b') as handle:
    try:
      fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      yield False
    else:
      yield True


def verified_binary(binary: Path) -> Path:
  """verify an executable against the checksum record beside it."""
  record = _checksum_record(binary)
  if not binary.is_file():
    raise ValueError(f'Claude Code binary is missing: {binary}')
  try:
    expected = record.read_text().strip()
  except OSError as error:
    raise ValueError(f'cannot read Claude Code checksum record {record}: {error}') from error
  if not _digest_valid(expected):
    raise ValueError(f'Claude Code checksum record {record} is malformed')
  actual = _file_digest(binary)
  if actual != expected:
    raise ValueError(
      f'Claude Code binary {binary} has digest {actual}, its checksum record states {expected}'
    )
  if not binary.stat().st_mode & 0o111:
    raise ValueError(f'Claude Code binary is not executable: {binary}')
  return binary


def _verified_cached_binary(version: str, platform_name: str) -> Path | None:
  binary = _binary(version, platform_name)
  if not binary.is_file() or not _checksum_record(binary).is_file():
    return None
  verified_binary(binary)
  return binary


def _record_checksum(binary: Path, expected: str, staging: Path) -> None:
  record = _checksum_record(binary)
  staged_record = staging / record.name
  staged_record.write_text(f'{expected}\n')
  staged_record.replace(record)


def cached_binary(version: str, platform_name: str) -> Path:
  """return a verified pinned binary while reserving its version for this process.

  A verified cached copy is checked against its adjacent checksum record without
  reading the release site. A copy that differs from that record is refused.
  """
  _reserve_version(version)
  verified = _verified_cached_binary(version, platform_name)
  if verified is not None:
    return verified

  with _download_lock(version):
    verified = _verified_cached_binary(version, platform_name)
    if verified is not None:
      return verified

    release = f'{RELEASES_URL}/{version}'
    expected = checksum(_fetch_json(f'{release}/manifest.json'), platform_name)
    binary = _binary(version, platform_name)
    binary.parent.mkdir(parents=True, exist_ok=True)
    if binary.is_file() and _file_digest(binary) == expected:
      binary.chmod(0o755)
      with tempfile.TemporaryDirectory(prefix='.checksum-', dir=binary.parent) as directory:
        _record_checksum(binary, expected, Path(directory))
      return binary

    log.info('downloading Claude Code %s for %s', version, platform_name)
    with tempfile.TemporaryDirectory(prefix='.download-', dir=binary.parent) as directory:
      staging = Path(directory)
      staged_binary = staging / 'claude'
      _download(f'{release}/{platform_name}/claude', staged_binary)
      actual = _file_digest(staged_binary)
      if actual != expected:
        raise ValueError(
          f'Claude Code {version} for {platform_name} downloaded with digest {actual}, '
          f'the release manifest states {expected}'
        )
      staged_binary.chmod(0o755)
      staged_binary.replace(binary)
      _record_checksum(binary, expected, staging)
    return binary


def clean_cached_releases(*, dry_run: bool = False) -> tuple[int, int]:
  """remove unreserved cached versions, returning removed and active counts."""
  root = cache_root()
  if not root.is_dir():
    return 0, 0
  removed = 0
  active = 0
  for directory in sorted(root.iterdir()):
    if not directory.is_dir():
      raise ValueError(f'unexpected file in the Claude Code cache: {directory}')
    version = _component(directory.name, 'version')
    with _removal_lock(version) as reserved:
      if not reserved:
        active += 1
      elif directory.exists():
        if not dry_run:
          shutil.rmtree(directory)
        removed += 1
  return removed, active
