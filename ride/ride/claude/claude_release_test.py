import hashlib
from pathlib import Path

import pytest

import ride.claude.claude_release as release
from ride.claude.claude_release import RELEASES_URL, cached_binary, checksum, host_platform

_PLATFORM = 'linux-x64'


def _release_manifest(stated: str) -> dict[str, object]:
  return {'version': '2.1.258', 'platforms': {_PLATFORM: {'checksum': stated}}}


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
def releases(monkeypatch):
  """the Claude Code release site, answering the manifest and a binary."""
  binary = b'#!/bin/sh\necho claude\n'
  stated = hashlib.sha256(binary).hexdigest()
  fetched: list[str] = []
  monkeypatch.setattr(
    release, '_fetch_json', lambda url: fetched.append(url) or _release_manifest(stated)
  )

  def download(url: str, into: Path) -> None:
    fetched.append(url)
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes(binary)

  monkeypatch.setattr(release, '_download', download)
  return binary, fetched


def test_a_binary_is_downloaded_once_verified_and_executable(tmp_path, releases):
  binary, fetched = releases

  first = cached_binary('2.1.258', _PLATFORM, tmp_path / 'cache')
  second = cached_binary('2.1.258', _PLATFORM, tmp_path / 'cache')

  assert first == second
  assert first.read_bytes() == binary
  assert first.stat().st_mode & 0o111 != 0
  assert fetched == [
    f'{RELEASES_URL}/2.1.258/manifest.json',
    f'{RELEASES_URL}/2.1.258/{_PLATFORM}/claude',
    f'{RELEASES_URL}/2.1.258/manifest.json',
  ]


def test_a_cached_binary_off_the_release_checksum_is_replaced(tmp_path, releases):
  binary, fetched = releases
  stale = cached_binary('2.1.258', _PLATFORM, tmp_path / 'cache')
  stale.write_bytes(b'stale')

  assert cached_binary('2.1.258', _PLATFORM, tmp_path / 'cache').read_bytes() == binary
  assert len(fetched) == 4


def test_a_download_off_the_release_checksum_is_refused(tmp_path, releases, monkeypatch):
  monkeypatch.setattr(release, '_fetch_json', lambda url: _release_manifest('0' * 64))

  with pytest.raises(ValueError, match='release manifest states'):
    cached_binary('2.1.258', _PLATFORM, tmp_path / 'cache')
  assert [path for path in (tmp_path / 'cache').rglob('*') if path.is_file()] == []
