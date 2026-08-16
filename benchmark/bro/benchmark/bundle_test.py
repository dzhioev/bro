import platform
import subprocess
from pathlib import Path

import pytest

from bro.benchmark import bundle as bundle_module
from bro.benchmark.bundle import (
  CPYTHON_VERSION,
  Bundle,
  build,
  built,
  default_root,
  export_command,
  host_mismatch,
  install_command,
  python_install_command,
  shim_text,
  wheel_command,
  workspace_root,
)


def _fake_bundle(root: Path) -> Bundle:
  """a bundle whose parts exist, with a shell script standing in for CPython."""
  bundle = Bundle(root)
  bundle.interpreter.parent.mkdir(parents=True)
  bundle.interpreter.write_text('#!/bin/sh\necho "PYTHONPATH=$PYTHONPATH"\necho "argv=$*"\n')
  bundle.interpreter.chmod(0o755)
  bundle.command.parent.mkdir(parents=True)
  bundle.command.write_text('')
  bundle.ca_bundle.parent.mkdir(parents=True)
  bundle.ca_bundle.write_text('')
  bundle.shim.write_text(shim_text(bundle))
  bundle.shim.chmod(0o755)
  return bundle


def test_layout_hangs_off_the_root(tmp_path):
  bundle = Bundle(tmp_path / 'bundle')

  assert bundle.shim == tmp_path / 'bundle' / 'bro'
  assert bundle.interpreter == tmp_path / 'bundle' / 'python' / 'bin' / 'python3'
  assert bundle.site_packages == tmp_path / 'bundle' / 'site-packages'
  assert bundle.command == tmp_path / 'bundle' / 'site-packages' / 'bin' / 'bro'
  assert bundle.ca_bundle == tmp_path / 'bundle' / 'site-packages' / 'certifi' / 'cacert.pem'


def test_built_names_the_build_command_for_an_absent_bundle(tmp_path):
  with pytest.raises(FileNotFoundError, match='benchmark-bundle'):
    built(tmp_path / 'absent')


def test_built_reports_every_missing_part(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')
  bundle.ca_bundle.unlink()

  assert bundle.missing() == (bundle.ca_bundle,)
  with pytest.raises(FileNotFoundError, match='cacert.pem'):
    built(bundle.root)


def test_built_accepts_a_complete_bundle(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  assert built(bundle.root) == bundle


def test_the_shim_runs_the_framework_through_the_bundled_interpreter(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  result = subprocess.run(
    ['./bundle/bro', 'show', 'terminal'],
    capture_output=True,
    text=True,
    cwd=tmp_path,
    check=True,
  )

  assert f'PYTHONPATH={bundle.site_packages}' in result.stdout
  assert f'argv=-s {bundle.command} show terminal' in result.stdout


def test_the_shim_follows_a_relocated_bundle(tmp_path):
  _fake_bundle(tmp_path / 'built')
  moved = Bundle(tmp_path / 'elsewhere')
  (tmp_path / 'built').rename(moved.root)

  result = subprocess.run([str(moved.shim)], capture_output=True, text=True, check=True)

  assert f'PYTHONPATH={moved.site_packages}' in result.stdout


def test_the_shim_keeps_an_inherited_python_path(tmp_path):
  bundle = _fake_bundle(tmp_path / 'bundle')

  result = subprocess.run(
    [str(bundle.shim)],
    capture_output=True,
    text=True,
    check=True,
    env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '/outer'},
  )

  assert f'PYTHONPATH={bundle.site_packages}:/outer' in result.stdout


def test_the_host_this_suite_runs_on_can_build():
  assert host_mismatch() is None


def test_an_unrecognised_libc_is_named(monkeypatch):
  monkeypatch.setattr(platform, 'libc_ver', lambda: ('', ''))

  assert (
    host_mismatch()
    == 'the bundle targets linux/x86_64/glibc; this host is linux/x86_64/unrecognised-libc'
  )


def test_a_foreign_architecture_is_named(monkeypatch):
  monkeypatch.setattr(platform, 'machine', lambda: 'aarch64')

  mismatch = host_mismatch()

  assert mismatch is not None and 'aarch64' in mismatch


def test_a_build_refuses_a_host_it_does_not_target(monkeypatch, tmp_path):
  monkeypatch.setattr(bundle_module, 'host_mismatch', lambda: 'nope')

  with pytest.raises(RuntimeError, match='nope'):
    build(tmp_path / 'checkout', tmp_path / 'bundle')
  assert not (tmp_path / 'bundle').exists()


def test_the_interpreter_is_pinned():
  assert python_install_command(Path('/staging'))[-1] == CPYTHON_VERSION
  assert '--no-bin' in python_install_command(Path('/staging'))


def test_the_export_is_the_locked_agent_surface():
  command = export_command(Path('/checkout'))

  assert '--frozen' in command
  assert command[command.index('--extra') + 1] == 'agent'
  assert '--no-default-groups' in command
  assert '--no-emit-workspace' in command


def test_the_framework_enters_as_a_wheel():
  command = wheel_command(Path('/checkout'), Path('/staging'))

  assert '--wheel' in command
  assert command[command.index('--package') + 1] == 'bro'


def test_the_install_targets_the_bundle_with_nothing_resolved():
  bundle = Bundle(Path('/bundle'))

  command = install_command(bundle, Path('/staging/requirements.txt'), Path('/staging/bro.whl'))

  assert command[command.index('--target') + 1] == str(bundle.site_packages)
  assert command[command.index('--python') + 1] == str(bundle.interpreter)
  assert '--no-deps' in command
  assert command[-1] == '/staging/bro.whl'


def test_the_workspace_is_the_checkout_the_framework_runs_from():
  root = workspace_root()

  assert (root / 'uv.lock').is_file()
  assert default_root(root) == root / 'var' / 'benchmark' / 'bundle'


def test_a_framework_outside_a_checkout_has_no_workspace(monkeypatch, tmp_path):
  monkeypatch.setattr(bundle_module, 'SOURCE_ROOT', tmp_path / 'site-packages' / 'bro')

  with pytest.raises(FileNotFoundError, match='uv.lock'):
    workspace_root()
