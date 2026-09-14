import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from bro.benchmark import bundle as bundle_module
from bro.benchmark.bundle import (
  CLAUDE_CODE_PLATFORM,
  CPYTHON_VERSION,
  MANIFEST_FORMAT,
  TARGET,
  WHEEL_PACKAGES,
  Bundle,
  build,
  built,
  cached_claude_code,
  claude_code_cache,
  claude_code_checksum,
  default_root,
  export_command,
  host_mismatch,
  install_command,
  python_install_command,
  relocate_script,
  wheel_command,
  workspace_root,
)

# a script body that reports how it was run, standing in for a console script
_SCRIPT_BODY = 'import sys\nprint("argv=" + " ".join(sys.argv))\n'


def _pin_host(
  monkeypatch, system: str = 'linux', machine: str = 'x86_64', libc: str = 'glibc'
) -> None:
  """pin the three facts `host_mismatch` reads, so the machine running the suite
  decides nothing."""
  monkeypatch.setattr(sys, 'platform', system)
  monkeypatch.setattr(platform, 'machine', lambda: machine)
  monkeypatch.setattr(platform, 'libc_ver', lambda: (libc, ''))


def _manifest(**overrides: object) -> dict[str, object]:
  manifest: dict[str, object] = {
    'format': MANIFEST_FORMAT,
    'claude_code': {'version': '2.1.258', 'sha256': '3' * 64},
    'cpython': CPYTHON_VERSION,
    'requirements': 'certifi==1\n',
    'source_commit': 'a' * 40,
    'target': list(TARGET),
    'wheels': {'bro.whl': '1' * 64},
  }
  manifest.update(overrides)
  return manifest


def _fake_bundle(root: Path) -> Bundle:
  """a bundle whose parts exist: the running interpreter stands in for the
  bundled CPython, and `ride` is a relocated console script over it."""
  bundle = Bundle(root)
  bundle.interpreter.parent.mkdir(parents=True)
  bundle.interpreter.symlink_to(sys.executable)
  ride = bundle.script('ride')
  ride.write_text(f'#!{bundle.interpreter}\n{_SCRIPT_BODY}')
  assert relocate_script(ride, bundle.venv)
  ride.chmod(0o755)
  bundle.shims.mkdir()
  (bundle.shims / 'ride').symlink_to(Path('..') / 'venv' / 'bin' / 'ride')
  bundle.claude_dir.mkdir()
  bundle.claude.write_text('')
  bundle.manifest.write_text(json.dumps(_manifest()))
  return bundle


def test_layout_hangs_off_the_root(tmp_path):
  bundle = Bundle(tmp_path / 'bundle')

  assert bundle.venv == tmp_path / 'bundle' / 'venv'
  assert bundle.interpreter == tmp_path / 'bundle' / 'venv' / 'bin' / 'python3'
  assert bundle.script('ride') == tmp_path / 'bundle' / 'venv' / 'bin' / 'ride'
  assert bundle.shims == tmp_path / 'bundle' / 'bin'
  assert bundle.claude == tmp_path / 'bundle' / 'claude' / 'claude'
  assert bundle.manifest == tmp_path / 'bundle' / 'bundle.json'


def test_built_names_the_build_command_for_an_absent_bundle(tmp_path):
  with pytest.raises(FileNotFoundError, match='benchmark bundle'):
    built(tmp_path / 'absent')


def test_built_reports_every_missing_part(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.claude.unlink()

  assert bundle.missing() == (bundle.claude,)
  with pytest.raises(FileNotFoundError, match='claude'):
    built(bundle.root)


def test_built_accepts_a_complete_bundle(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  assert built(bundle.root) == bundle


def test_the_bundle_carries_its_source_commit(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  assert bundle.source_commit == 'a' * 40


def test_the_bundle_identity_changes_with_its_framework_wheels(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  first = bundle.identity
  bundle.manifest.write_text(json.dumps(_manifest(wheels={'bro.whl': '2' * 64})))

  assert first.startswith('sha256:')
  assert len(first) == len('sha256:') + 64
  assert bundle.identity != first


def test_the_bundle_identity_changes_with_its_claude_code(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  first = bundle.identity
  bundle.manifest.write_text(
    json.dumps(_manifest(claude_code={'version': '2.1.259', 'sha256': '4' * 64}))
  )

  assert bundle.identity != first


def test_built_refuses_a_malformed_manifest(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text('{')

  with pytest.raises(ValueError, match='invalid bundle manifest'):
    built(bundle.root)


@pytest.mark.parametrize(
  'manifest',
  [
    _manifest(format=2),
    _manifest(claude_code={'version': '', 'sha256': '3' * 64}),
    _manifest(claude_code={'version': '2.1.258'}),
  ],
)
def test_built_refuses_a_manifest_off_the_format(tmp_path, manifest):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text(json.dumps(manifest))

  with pytest.raises(ValueError, match='malformed values|unexpected fields'):
    built(bundle.root)


def test_a_relocated_script_runs_on_the_interpreter_beside_it(tmp_path):
  _fake_bundle(tmp_path / 'built')
  moved = Bundle(tmp_path / 'elsewhere')
  (tmp_path / 'built').rename(moved.root)

  result = subprocess.run(
    [str(moved.script('ride')), 'solo', 'terminal'], capture_output=True, text=True, check=True
  )

  assert result.stdout == f'argv={moved.script("ride")} solo terminal\n'


def test_the_shim_farm_leads_to_the_relocated_scripts(tmp_path):
  _fake_bundle(tmp_path / 'built')
  moved = Bundle(tmp_path / 'elsewhere')
  (tmp_path / 'built').rename(moved.root)

  result = subprocess.run(
    ['./bin/ride', 'list'], capture_output=True, text=True, check=True, cwd=moved.root
  )

  assert result.stdout == 'argv=./bin/ride list\n'


def test_relocation_rewrites_the_shebangs_the_installer_writes(tmp_path):
  venv = tmp_path / 'venv'
  short = tmp_path / 'short'
  short.write_text(f'#!{venv}/bin/python3\n{_SCRIPT_BODY}')
  long = tmp_path / 'long'
  long.write_text(
    f"#!/bin/sh\n'''exec' '{venv}/bin/python3.12' \"$0\" \"$@\"\n' '''\n{_SCRIPT_BODY}"
  )

  assert relocate_script(short, venv)
  assert relocate_script(long, venv)
  assert short.read_text() == long.read_text()
  assert short.read_text().startswith('#!/bin/sh\n')
  assert short.read_text().endswith(_SCRIPT_BODY)
  assert str(venv) not in short.read_text()


@pytest.mark.parametrize(
  'text',
  [
    '#!/install/bin/python3.12\nprint()\n',
    "#!/bin/sh\n'''exec' '/elsewhere/bin/python3' \"$0\" \"$@\"\n' '''\nprint()\n",
    '#!/bin/sh\necho hi\n',
    'print()\n',
  ],
)
def test_relocation_leaves_a_script_that_names_another_interpreter(tmp_path, text):
  script = tmp_path / 'script'
  script.write_text(text)

  assert not relocate_script(script, tmp_path / 'venv')
  assert script.read_text() == text


def test_the_targeted_host_can_build(monkeypatch):
  _pin_host(monkeypatch)

  assert host_mismatch() is None


def test_an_unrecognised_libc_is_named(monkeypatch):
  _pin_host(monkeypatch, libc='')

  assert (
    host_mismatch()
    == 'the bundle targets linux/x86_64/glibc; this host is linux/x86_64/unrecognised-libc'
  )


def test_a_foreign_architecture_is_named(monkeypatch):
  _pin_host(monkeypatch, machine='aarch64')

  assert (
    host_mismatch() == 'the bundle targets linux/x86_64/glibc; this host is linux/aarch64/glibc'
  )


def test_a_build_refuses_a_host_it_does_not_target(monkeypatch, tmp_path):
  monkeypatch.setattr(bundle_module, 'host_mismatch', lambda: 'nope')

  with pytest.raises(RuntimeError, match='nope'):
    build(tmp_path / 'checkout', tmp_path / 'bundle', tmp_path / 'cache')
  assert not (tmp_path / 'bundle').exists()


def test_the_interpreter_is_pinned():
  assert python_install_command(Path('/staging'))[-1] == CPYTHON_VERSION
  assert '--no-bin' in python_install_command(Path('/staging'))


def test_the_export_is_the_locked_surface_of_every_bundled_distribution():
  command = export_command(Path('/checkout'))

  assert '--frozen' in command
  selected = [command[index + 1] for index, item in enumerate(command) if item == '--package']
  assert selected == list(WHEEL_PACKAGES)
  assert '--no-default-groups' in command
  assert '--no-emit-workspace' in command


@pytest.mark.parametrize('package', WHEEL_PACKAGES)
def test_every_bundled_distribution_enters_as_a_wheel(package):
  command = wheel_command(Path('/checkout'), Path('/staging'), package)

  assert '--wheel' in command
  assert command[command.index('--package') + 1] == package


def test_the_install_targets_the_bundled_interpreter_with_nothing_resolved():
  bundle = Bundle(Path('/bundle'))
  wheels = [Path('/staging/bro.whl'), Path('/staging/bro_native.whl')]

  command = install_command(bundle, Path('/staging/requirements.txt'), wheels)

  assert command[command.index('--python') + 1] == str(bundle.interpreter)
  assert '--target' not in command
  assert '--no-deps' in command
  assert command[-2:] == ['/staging/bro.whl', '/staging/bro_native.whl']


def _release_manifest(checksum: str) -> dict[str, object]:
  return {'version': '2.1.258', 'platforms': {CLAUDE_CODE_PLATFORM: {'checksum': checksum}}}


def test_the_release_checksum_is_the_targeted_platforms():
  assert claude_code_checksum(_release_manifest('5' * 64)) == '5' * 64


@pytest.mark.parametrize(
  'manifest',
  [{}, {'platforms': {'darwin-arm64': {'checksum': '5' * 64}}}, _release_manifest('short')],
)
def test_a_release_manifest_without_the_platform_is_refused(manifest):
  with pytest.raises(ValueError, match=CLAUDE_CODE_PLATFORM):
    claude_code_checksum(manifest)


@pytest.fixture
def releases(monkeypatch):
  """the Claude Code release site, answering the manifest and a binary."""
  binary = b'#!/bin/sh\necho claude\n'
  checksum = hashlib.sha256(binary).hexdigest()
  fetched: list[str] = []
  monkeypatch.setattr(
    bundle_module, '_fetch_json', lambda url: fetched.append(url) or _release_manifest(checksum)
  )

  def download(url: str, into: Path) -> None:
    fetched.append(url)
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes(binary)

  monkeypatch.setattr(bundle_module, '_download', download)
  return binary, checksum, fetched


def test_a_binary_is_downloaded_once_and_verified(tmp_path, releases):
  binary, _, fetched = releases

  first = cached_claude_code('2.1.258', tmp_path / 'cache')
  second = cached_claude_code('2.1.258', tmp_path / 'cache')

  assert first == second == tmp_path / 'cache' / '2.1.258' / 'claude'
  assert first.read_bytes() == binary
  assert fetched == [
    f'{bundle_module.CLAUDE_CODE_RELEASES}/2.1.258/manifest.json',
    f'{bundle_module.CLAUDE_CODE_RELEASES}/2.1.258/{CLAUDE_CODE_PLATFORM}/claude',
    f'{bundle_module.CLAUDE_CODE_RELEASES}/2.1.258/manifest.json',
  ]


def test_a_cached_binary_off_the_release_checksum_is_replaced(tmp_path, releases):
  binary, _, fetched = releases
  stale = tmp_path / 'cache' / '2.1.258' / 'claude'
  stale.parent.mkdir(parents=True)
  stale.write_bytes(b'stale')

  assert cached_claude_code('2.1.258', tmp_path / 'cache').read_bytes() == binary
  assert len(fetched) == 2


def test_a_download_off_the_release_checksum_is_refused(tmp_path, releases, monkeypatch):
  monkeypatch.setattr(bundle_module, '_fetch_json', lambda url: _release_manifest('0' * 64))

  with pytest.raises(ValueError, match='release manifest states'):
    cached_claude_code('2.1.258', tmp_path / 'cache')
  assert list((tmp_path / 'cache').rglob('*')) == [tmp_path / 'cache' / '2.1.258']


def test_the_workspace_is_the_checkout_the_framework_runs_from():
  root = workspace_root()

  assert (root / 'uv.lock').is_file()
  assert default_root(root) == root / 'var' / 'benchmark' / 'bundle'
  assert claude_code_cache(root) == root / 'var' / 'benchmark' / 'claude-code'


def test_a_framework_outside_a_checkout_has_no_workspace(monkeypatch, tmp_path):
  monkeypatch.setattr(bundle_module, 'SOURCE_ROOT', tmp_path / 'site-packages' / 'bro')

  with pytest.raises(FileNotFoundError, match='uv.lock'):
    workspace_root()


def test_the_shim_farm_is_relative_to_the_bundle(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  assert not Path(os.readlink(bundle.shims / 'ride')).is_absolute()
