"""Claude Code's native releases: a version's standalone binary for one platform,
downloaded from the release site and verified against the checksum its release
manifest states."""

import hashlib
import json
import platform
import shutil
import sys
import urllib.request
from pathlib import Path

from bro.base import log

RELEASES_URL = 'https://downloads.claude.ai/claude-code-releases'


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
  into.parent.mkdir(parents=True, exist_ok=True)
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


def cached_binary(version: str, platform_name: str, cache: Path) -> Path:
  """the Claude Code binary of `version` for `platform_name`, downloaded into
  `cache` unless a copy matching the release manifest's checksum is already there."""
  release = f'{RELEASES_URL}/{version}'
  expected = checksum(_fetch_json(f'{release}/manifest.json'), platform_name)
  binary = cache / version / platform_name / 'claude'
  if binary.is_file() and _file_digest(binary) == expected:
    return binary
  log.info('downloading Claude Code %s for %s', version, platform_name)
  staged = binary.with_suffix('.download')
  _download(f'{release}/{platform_name}/claude', staged)
  digest = _file_digest(staged)
  if digest != expected:
    staged.unlink()
    raise ValueError(
      f'Claude Code {version} for {platform_name} downloaded with digest {digest}, '
      f'the release manifest states {expected}'
    )
  staged.chmod(0o755)
  staged.replace(binary)
  return binary
