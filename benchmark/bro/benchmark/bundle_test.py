import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bro.benchmark import bundle as bundle_module
from bro.benchmark.bundle import (
  CPYTHON_VERSION,
  MANIFEST_FORMAT,
  TARGET,
  WHEEL_PACKAGES,
  Bundle,
  StaleBundleError,
  build,
  built,
  cached,
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
    'harness_files': {'bro': {}, 'claude': {'engine/binary': hashlib.sha256(b'').hexdigest()}},
    'cpython': CPYTHON_VERSION,
    'harnesses': ['bro', 'claude'],
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
  asset = root / 'engine' / 'binary'
  asset.parent.mkdir()
  asset.write_bytes(b'')
  bundle.manifest.write_text(json.dumps(_manifest()))
  return bundle


def test_layout_hangs_off_the_root(tmp_path):
  bundle = Bundle(tmp_path / 'bundle')

  assert bundle.venv == tmp_path / 'bundle' / 'venv'
  assert bundle.interpreter == tmp_path / 'bundle' / 'venv' / 'bin' / 'python3'
  assert bundle.script('ride') == tmp_path / 'bundle' / 'venv' / 'bin' / 'ride'
  assert bundle.shims == tmp_path / 'bundle' / 'bin'
  assert bundle.manifest == tmp_path / 'bundle' / 'bundle.json'


def test_built_refuses_an_absent_bundle_as_stale(tmp_path):
  with pytest.raises(StaleBundleError, match='no bundle was built'):
    built(tmp_path / 'absent')


def test_built_reports_every_missing_part(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  asset = bundle.root / 'engine' / 'binary'
  asset.unlink()

  assert bundle.missing() == (asset,)
  with pytest.raises(FileNotFoundError, match='engine/binary'):
    built(bundle.root)


def test_built_checks_harness_asset_contents(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  (bundle.root / 'engine' / 'binary').write_bytes(b'corrupted')

  with pytest.raises(ValueError, match='checksum mismatch'):
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


def test_the_bundle_identity_changes_with_its_harness_assets(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  first = bundle.identity
  bundle.manifest.write_text(
    json.dumps(_manifest(harness_files={'bro': {}, 'claude': {'engine/binary': '4' * 64}}))
  )

  assert bundle.identity != first


def test_built_refuses_a_malformed_manifest(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text('{')

  with pytest.raises(ValueError, match='invalid bundle manifest'):
    built(bundle.root)


def test_built_refuses_a_manifest_in_another_format_as_stale(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text(json.dumps(_manifest(format=MANIFEST_FORMAT - 1)))

  with pytest.raises(StaleBundleError, match='uses format'):
    built(bundle.root)


@pytest.mark.parametrize(
  'manifest',
  [
    _manifest(harness_files={'bro': {}, 'claude': {'engine/binary': 'bad'}}),
    _manifest(harness_files={'bro': {}}),
  ],
)
def test_built_refuses_a_manifest_off_the_format(tmp_path, manifest):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text(json.dumps(manifest))

  with pytest.raises(ValueError, match='malformed values|unexpected fields') as refused:
    built(bundle.root)
  assert not isinstance(refused.value, StaleBundleError)


def _record_builds(monkeypatch, workspace: Path) -> list[Path]:
  """stand a fake bundle in for every build from `workspace`, answering with
  the roots built."""
  roots: list[Path] = []

  def fake_build(source: Path, root: Path) -> Bundle:
    assert source == workspace
    roots.append(root)
    if root.exists():
      shutil.rmtree(root)
    return _fake_bundle(root)

  monkeypatch.setattr(bundle_module, 'build', fake_build)
  return roots


def test_cached_reuses_a_readable_bundle(tmp_path, monkeypatch):
  bundle = _fake_bundle(tmp_path / 'bundle')
  roots = _record_builds(monkeypatch, tmp_path / 'checkout')

  assert cached(tmp_path / 'checkout', bundle.root) == bundle
  assert roots == []


def test_cached_builds_an_absent_bundle(tmp_path, monkeypatch):
  root = tmp_path / 'bundle'
  roots = _record_builds(monkeypatch, tmp_path / 'checkout')

  assert cached(tmp_path / 'checkout', root) == Bundle(root)
  assert roots == [root]


def test_cached_rebuilds_a_bundle_in_another_manifest_format(tmp_path, monkeypatch):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text(json.dumps(_manifest(format=MANIFEST_FORMAT - 1)))
  roots = _record_builds(monkeypatch, tmp_path / 'checkout')

  assert cached(tmp_path / 'checkout', bundle.root) == bundle
  assert roots == [bundle.root]


def test_cached_refuses_a_damaged_bundle_without_rebuilding_it(tmp_path, monkeypatch):
  bundle = _fake_bundle(tmp_path / 'bundle')
  (bundle.root / 'engine' / 'binary').write_bytes(b'corrupted')
  roots = _record_builds(monkeypatch, tmp_path / 'checkout')

  with pytest.raises(ValueError, match='checksum mismatch'):
    cached(tmp_path / 'checkout', bundle.root)
  assert roots == []


@pytest.mark.parametrize(
  'manifest',
  [
    {key: value for key, value in _manifest().items() if key != 'format'},
    _manifest(format=None),
    _manifest(format='broken'),
    _manifest(format={}),
    _manifest(format=True),
  ],
)
def test_cached_refuses_a_malformed_format_declaration_without_rebuilding_it(
  tmp_path, monkeypatch, manifest
):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text(json.dumps(manifest))
  roots = _record_builds(monkeypatch, tmp_path / 'checkout')

  with pytest.raises(ValueError, match='malformed format'):
    cached(tmp_path / 'checkout', bundle.root)
  assert roots == []


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
    build(tmp_path / 'checkout', tmp_path / 'bundle')
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


def test_the_workspace_is_the_checkout_the_framework_runs_from():
  root = workspace_root()

  assert (root / 'uv.lock').is_file()
  assert default_root(root) == root / 'var' / 'benchmark' / 'bundle'


def test_a_framework_outside_a_checkout_has_no_workspace(monkeypatch, tmp_path):
  monkeypatch.setattr(bundle_module, 'SOURCE_ROOT', tmp_path / 'site-packages' / 'bro')

  with pytest.raises(FileNotFoundError, match='uv.lock'):
    workspace_root()


def test_the_shim_farm_is_relative_to_the_bundle(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  assert not Path(os.readlink(bundle.shims / 'ride')).is_absolute()


def test_a_single_harness_bundle_requires_no_other_harness_assets(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  (bundle.root / 'engine' / 'binary').unlink()
  bundle.manifest.write_text(json.dumps(_manifest(harnesses=['bro'], harness_files={'bro': {}})))

  assert built(bundle.root).harnesses == ('bro',)


def test_provisioning_runs_the_bundled_registry(monkeypatch, tmp_path):
  bundle = Bundle(tmp_path)
  calls = []

  def capture(command):
    calls.append(command)
    if '-c' in command:
      return '["third-party"]'
    return '{"third-party": {}}'

  monkeypatch.setattr(bundle_module, '_capture', capture)

  assert bundle_module._provision_harnesses(bundle) == {'third-party': {}}
  assert calls[0][:3] == [str(bundle.interpreter), '-m', 'ride.provisioning']
  assert calls[0][3:] == ['--bundle', str(tmp_path), '--target', *TARGET]


@pytest.mark.parametrize('path', ['/absolute', '../outside', 'venv/config', 'bin/tool'])
def test_manifest_refuses_harness_files_outside_the_asset_tree(tmp_path, path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.manifest.write_text(
    json.dumps(_manifest(harness_files={'bro': {}, 'claude': {path: '3' * 64}}))
  )
  with pytest.raises(ValueError, match='harness asset'):
    built(bundle.root)


@pytest.mark.parametrize('parent', [False, True])
@pytest.mark.parametrize('absolute', [False, True])
def test_built_refuses_non_regular_harness_assets_before_relocation(tmp_path, parent, absolute):
  bundle = _fake_bundle(tmp_path / 'bundle')
  payload = bundle.root / 'engine'
  alias = bundle.root / 'alias'
  target = payload if parent else payload / 'binary'
  alias.symlink_to(target if absolute else target.relative_to(bundle.root))
  relative = 'alias/binary' if parent else 'alias'
  bundle.manifest.write_text(
    json.dumps(
      _manifest(harness_files={'bro': {}, 'claude': {relative: hashlib.sha256(b'').hexdigest()}})
    )
  )

  with pytest.raises(ValueError, match='symbolic links'):
    built(bundle.root)


def test_regular_harness_assets_survive_bundle_relocation(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  original_identity = built(bundle.root).identity
  moved = tmp_path / 'uploaded'
  bundle.root.rename(moved)

  assert built(moved).identity == original_identity
  assert (moved / 'engine' / 'binary').read_bytes() == b''
