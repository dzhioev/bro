import contextlib
import hashlib
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import ride.claude.claude_release as release
from ride.claude.claude_release import (
  RELEASES_URL,
  cached_binary,
  checksum,
  clean_cached_releases,
  host_platform,
  seed_binary,
)

_PLATFORM = 'linux-x64'
_VERSION = '2.1.258'


def _release_manifest(stated: str) -> dict[str, object]:
  return {'version': _VERSION, 'platforms': {_PLATFORM: {'checksum': stated}}}


def _cached_copy(root: Path, version: str, content: bytes) -> Path:
  binary = root / 'claude-code' / version / _PLATFORM / 'claude'
  binary.parent.mkdir(parents=True)
  binary.write_bytes(content)
  binary.chmod(0o755)
  binary.with_suffix('.sha256').write_text(f'{hashlib.sha256(content).hexdigest()}\n')
  return binary


@pytest.fixture
def runtime_root(monkeypatch, tmp_path) -> Path:
  root = tmp_path / 'runtime'
  monkeypatch.setattr(release, 'runtime_base', lambda: root)
  return root


def test_the_checksum_is_the_named_platforms():
  assert checksum(_release_manifest('5' * 64), _PLATFORM) == '5' * 64


@pytest.mark.parametrize(
  'manifest',
  [{}, {'platforms': {'darwin-arm64': {'checksum': '5' * 64}}}, _release_manifest('short')],
)
def test_a_release_manifest_without_the_platform_is_refused(manifest):
  with pytest.raises(ValueError, match=_PLATFORM):
    checksum(manifest, _PLATFORM)


@pytest.mark.parametrize(
  ('system', 'machine', 'libc', 'expected'),
  [
    ('linux', 'x86_64', 'glibc', 'linux-x64'),
    ('linux', 'aarch64', 'glibc', 'linux-arm64'),
    ('darwin', 'arm64', '', 'darwin-arm64'),
  ],
)
def test_the_host_platform_is_named_as_the_release_manifest_names_it(
  monkeypatch, system, machine, libc, expected
):
  monkeypatch.setattr(release.sys, 'platform', system)
  monkeypatch.setattr(release.platform, 'machine', lambda: machine)
  monkeypatch.setattr(release.platform, 'libc_ver', lambda: (libc, ''))

  assert host_platform() == expected


@pytest.mark.parametrize(
  ('system', 'machine', 'libc'),
  [('win32', 'AMD64', ''), ('linux', 'riscv64', 'glibc'), ('linux', 'x86_64', '')],
)
def test_a_host_outside_the_selectable_platforms_is_refused(monkeypatch, system, machine, libc):
  monkeypatch.setattr(release.sys, 'platform', system)
  monkeypatch.setattr(release.platform, 'machine', lambda: machine)
  monkeypatch.setattr(release.platform, 'libc_ver', lambda: (libc, ''))

  with pytest.raises(ValueError):
    host_platform()


@pytest.fixture
def release_site(monkeypatch):
  """the Claude Code release site, answering the manifest and a binary."""
  binary = b'#!/bin/sh\necho claude\n'
  stated = hashlib.sha256(binary).hexdigest()
  fetched: list[str] = []
  monkeypatch.setattr(
    release, '_fetch_json', lambda url: fetched.append(url) or _release_manifest(stated)
  )

  def download(url: str, into: Path) -> None:
    fetched.append(url)
    into.write_bytes(binary)

  monkeypatch.setattr(release, '_download', download)
  return binary, fetched


def test_a_carried_binary_is_verified_against_its_record(tmp_path):
  binary = _cached_copy(tmp_path, _VERSION, b'carried')

  assert release.verified_binary(binary) == binary
  binary.write_bytes(b'changed')
  with pytest.raises(ValueError, match='checksum record states'):
    release.verified_binary(binary)


def test_a_verified_copy_is_reused_offline(runtime_root, release_site, monkeypatch):
  binary_content, fetched = release_site
  binary = cached_binary(_VERSION, _PLATFORM)
  monkeypatch.setattr(
    release,
    '_fetch_json',
    lambda _url: pytest.fail('a verified cached copy must not read the release manifest'),
  )

  reused = cached_binary(_VERSION, _PLATFORM)

  assert reused == binary
  assert reused.read_bytes() == binary_content
  assert reused.stat().st_mode & 0o111 != 0
  assert (
    reused.with_suffix('.sha256').read_text() == f'{hashlib.sha256(binary_content).hexdigest()}\n'
  )
  assert fetched == [
    f'{RELEASES_URL}/{_VERSION}/manifest.json',
    f'{RELEASES_URL}/{_VERSION}/{_PLATFORM}/claude',
  ]


def test_concurrent_first_starts_download_once(runtime_root, release_site, monkeypatch):
  binary_content, fetched = release_site
  download_started = threading.Event()
  release_download = threading.Event()

  def download(url: str, into: Path) -> None:
    fetched.append(url)
    download_started.set()
    assert release_download.wait(timeout=5)
    into.write_bytes(binary_content)

  monkeypatch.setattr(release, '_download', download)
  with ThreadPoolExecutor(max_workers=2) as executor:
    first = executor.submit(cached_binary, _VERSION, _PLATFORM)
    assert download_started.wait(timeout=5)
    second = executor.submit(cached_binary, _VERSION, _PLATFORM)
    release_download.set()
    paths = [first.result(timeout=5), second.result(timeout=5)]

  assert paths[0] == paths[1]
  assert fetched == [
    f'{RELEASES_URL}/{_VERSION}/manifest.json',
    f'{RELEASES_URL}/{_VERSION}/{_PLATFORM}/claude',
  ]


def test_a_cached_copy_changed_after_verification_is_refused(runtime_root, release_site):
  _binary_content, fetched = release_site
  binary = cached_binary(_VERSION, _PLATFORM)
  binary.write_bytes(b'tampered')

  with pytest.raises(ValueError, match='checksum record states'):
    cached_binary(_VERSION, _PLATFORM)

  assert len(fetched) == 2


def test_a_download_off_the_release_checksum_is_refused(runtime_root, release_site, monkeypatch):
  monkeypatch.setattr(release, '_fetch_json', lambda url: _release_manifest('0' * 64))

  with pytest.raises(ValueError, match='release manifest states'):
    cached_binary(_VERSION, _PLATFORM)

  assert [path for path in release.cache_root().rglob('*') if path.is_file()] == []


def test_a_seeded_copy_is_reused_offline(runtime_root, monkeypatch, tmp_path):
  source = _cached_copy(tmp_path / 'source', _VERSION, b'seeded')
  monkeypatch.setattr(
    release,
    '_fetch_json',
    lambda _url: pytest.fail('a seeded copy must not read the release manifest'),
  )

  seeded = seed_binary(source, _VERSION, _PLATFORM)

  assert cached_binary(_VERSION, _PLATFORM) == seeded
  assert seeded.read_bytes() == b'seeded'
  assert seeded.stat().st_mode & 0o111 != 0


def test_seeding_keeps_a_verified_cached_copy(runtime_root, release_site, tmp_path):
  binary_content, _fetched = release_site
  cached = cached_binary(_VERSION, _PLATFORM)
  source = _cached_copy(tmp_path / 'source', _VERSION, b'another build')

  assert seed_binary(source, _VERSION, _PLATFORM) == cached
  assert cached.read_bytes() == binary_content


def test_a_cleanup_during_a_seed_keeps_the_seeded_version(runtime_root, monkeypatch, tmp_path):
  source = _cached_copy(tmp_path / 'source', _VERSION, b'seeded')
  copy = release.shutil.copyfile
  cleanups: list[tuple[int, int]] = []

  def copy_after_a_cleanup(source_path: Path, destination: Path) -> Path:
    cleanups.append(clean_cached_releases())
    return copy(source_path, destination)

  monkeypatch.setattr(release.shutil, 'copyfile', copy_after_a_cleanup)

  seeded = seed_binary(source, _VERSION, _PLATFORM)

  assert cleanups == [(0, 1)]
  assert seeded.read_bytes() == b'seeded'


def test_a_source_off_its_checksum_record_is_not_seeded(runtime_root, tmp_path):
  source = _cached_copy(tmp_path / 'source', _VERSION, b'recorded')
  source.write_bytes(b'changed')

  with pytest.raises(ValueError, match='checksum record states'):
    seed_binary(source, _VERSION, _PLATFORM)

  assert [path for path in release.cache_root().rglob('*') if path.is_file()] == []


@contextlib.contextmanager
def _held_release(data_home: Path, version: str) -> Iterator[subprocess.Popen[str]]:
  source = """
import os
from ride.claude.claude_release import cached_binary
cached_binary(os.environ['CLAUDE_VERSION'], os.environ['CLAUDE_PLATFORM'])
print('ready', flush=True)
input()
"""
  process = subprocess.Popen(
    [sys.executable, '-c', source],
    env={
      **os.environ,
      'XDG_DATA_HOME': str(data_home),
      'CLAUDE_VERSION': version,
      'CLAUDE_PLATFORM': _PLATFORM,
    },
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
  )
  try:
    assert process.stdout is not None
    assert process.stdout.readline() == 'ready\n'
    yield process
  finally:
    if process.poll() is None:
      assert process.stdin is not None
      process.stdin.write('\n')
      process.stdin.flush()
    process.wait(timeout=5)


def test_clean_keeps_a_version_held_by_a_session(monkeypatch, tmp_path):
  data_home = tmp_path / 'data'
  root = data_home / 'ride'
  held = _cached_copy(root, '2.1.258', b'held')
  removable = _cached_copy(root, '2.1.257', b'removable')
  monkeypatch.setattr(release, 'runtime_base', lambda: root)

  with _held_release(data_home, '2.1.258'):
    assert clean_cached_releases() == (1, 1)
    assert held.is_file()
    assert not removable.parent.parent.exists()

  assert clean_cached_releases() == (1, 0)
  assert not held.parent.parent.exists()
