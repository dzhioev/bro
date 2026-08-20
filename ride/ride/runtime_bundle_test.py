import errno
import fcntl
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import ride.runtime_bundle as runtime_bundle


class _Distribution:
  def __init__(self, name: str, version: str, direct_url: dict | None = None):
    self.metadata = {'Name': name}
    self.version = version
    self._direct_url = direct_url

  def read_text(self, filename: str):
    assert filename == 'direct_url.json'
    return None if self._direct_url is None else json.dumps(self._direct_url)


def _pure_wheel(path: Path) -> None:
  with zipfile.ZipFile(path, 'w') as archive:
    archive.writestr(
      'demo-1.0.dist-info/WHEEL',
      'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    )


def test_classifies_index_and_local_distributions(monkeypatch, tmp_path):
  local = tmp_path / 'local'
  local.mkdir()
  distributions = [
    _Distribution('Index-Package', '2.0'),
    _Distribution('Local_Package', '1.0', {'url': local.as_uri(), 'dir_info': {'editable': True}}),
  ]
  monkeypatch.setattr(runtime_bundle.importlib.metadata, 'distributions', lambda: distributions)

  python, pins, local_distributions = runtime_bundle._classify_installation()

  assert python == f'{sys.version_info.major}.{sys.version_info.minor}'
  assert pins == ['Index-Package==2.0']
  assert local_distributions == [runtime_bundle._LocalDistribution('Local_Package', local)]


def test_rejects_a_direct_archive_install(monkeypatch, tmp_path):
  archive = tmp_path / 'package.whl'
  archive.touch()
  monkeypatch.setattr(
    runtime_bundle.importlib.metadata,
    'distributions',
    lambda: [_Distribution('archive', '1', {'url': archive.as_uri()})],
  )

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='not a directory'):
    runtime_bundle._classify_installation()


def test_bundle_hash_covers_pins_python_and_wheel_bytes(tmp_path):
  wheel = tmp_path / 'demo.whl'
  wheel.write_bytes(b'wheel one')
  first_manifest = runtime_bundle._manifest('3.12', ['a==1'], [wheel])
  first = runtime_bundle._persist_bundle(tmp_path, first_manifest, [wheel])
  assert runtime_bundle._persist_bundle(tmp_path, first_manifest, [wheel]) == first

  wheel.write_bytes(b'wheel two')
  second_manifest = runtime_bundle._manifest('3.12', ['a==1'], [wheel])
  second = runtime_bundle._persist_bundle(tmp_path, second_manifest, [wheel])

  assert first != second
  assert (first / 'pins.txt').read_text() == 'a==1\n'
  assert (second / 'wheels' / 'demo.whl').read_bytes() == b'wheel two'


def test_bundle_persistence_reuses_a_concurrent_winner(monkeypatch, tmp_path):
  wheel = tmp_path / 'demo.whl'
  wheel.write_bytes(b'wheel')
  manifest = runtime_bundle._manifest('3.12', ['a==1'], [wheel])
  original_rename = Path.rename

  def rename_after_competitor_wins(source: Path, target: Path):
    shutil.copytree(source, target)
    return original_rename(source, target)

  monkeypatch.setattr(Path, 'rename', rename_after_competitor_wins)

  root = runtime_bundle._persist_bundle(tmp_path, manifest, [wheel])

  assert root.is_dir()
  assert (root / 'wheels' / wheel.name).read_bytes() == b'wheel'


def test_bundle_persistence_propagates_unexpected_rename_errors(monkeypatch, tmp_path):
  wheel = tmp_path / 'demo.whl'
  wheel.write_bytes(b'wheel')
  manifest = runtime_bundle._manifest('3.12', ['a==1'], [wheel])

  def refuse_rename(_source: Path, _target: Path):
    raise OSError(errno.EACCES, 'permission denied')

  monkeypatch.setattr(Path, 'rename', refuse_rename)

  with pytest.raises(OSError, match='permission denied'):
    runtime_bundle._persist_bundle(tmp_path, manifest, [wheel])


def test_rejects_a_platform_wheel(tmp_path):
  wheel = tmp_path / 'demo.whl'
  with zipfile.ZipFile(wheel, 'w') as archive:
    archive.writestr(
      'demo-1.0.dist-info/WHEEL',
      'Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n',
    )

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='pure-Python'):
    runtime_bundle._assert_pure_wheel(wheel)


def test_materializer_installs_exact_snapshot_checks_closure_and_builds_shims(
  monkeypatch, tmp_path
):
  bundle = tmp_path / 'bundle'
  wheels = bundle / 'wheels'
  wheels.mkdir(parents=True)
  wheel = wheels / 'demo.whl'
  _pure_wheel(wheel)
  (bundle / 'pins.txt').write_text('index-package==1\n')
  commands: list[list[str]] = []

  def run(command, *, description):
    del description
    commands.append(command)
    if command[:2] == ['uv', 'venv']:
      (Path(command[-1]) / 'bin').mkdir(parents=True)
      (Path(command[-1]) / 'bin' / 'python').touch()
      (Path(command[-1]) / 'bin' / 'summon').touch()
    elif command[0] == 'mkdir':
      Path(command[1]).mkdir()
    elif command[0] == 'ln':
      Path(command[-1]).symlink_to(command[-2])
    return subprocess.CompletedProcess(command, 0, '', '')

  monkeypatch.setattr(runtime_bundle, '_run', run)
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python, run=None: ['summon'])
  host = bundle / 'host'
  host.mkdir()

  runtime_bundle._materialize(bundle, host, sys.executable)

  assert commands[0][:4] == ['uv', 'venv', '--python', sys.executable]
  assert '--no-deps' in commands[1]
  assert str(bundle / 'pins.txt') in commands[1]
  assert str(wheel) in commands[1]
  assert commands[2][:3] == ['uv', 'pip', 'check']
  assert (host / 'bin' / 'summon').resolve() == host / 'venv' / 'bin' / 'summon'


def test_container_materialization_populates_a_named_volume_once(monkeypatch, tmp_path):
  root = tmp_path / ('a' * 64)
  (root / 'wheels').mkdir(parents=True)
  bundle = runtime_bundle.RuntimeBundle(root, '3.12')
  calls: list[list[str]] = []
  materialized: list[tuple] = []

  def run(command, *args, **kwargs):
    del args, kwargs
    calls.append(command)
    if command[:2] == ['docker', 'create']:
      return subprocess.CompletedProcess(command, 0, 'container-id\n', '')
    if command[:5] == ['docker', 'exec', 'container-id', 'test', '-f']:
      return subprocess.CompletedProcess(command, 1, '', '')
    return subprocess.CompletedProcess(command, 0, '', '')

  monkeypatch.setattr(runtime_bundle.subprocess, 'run', run)
  monkeypatch.setattr(
    runtime_bundle,
    '_materialize',
    lambda *args, **kwargs: materialized.append((args, kwargs)),
  )

  bundle.materialize_container('runtime-image')

  assert calls[0][:3] == ['docker', 'volume', 'create']
  create = next(command for command in calls if command[:2] == ['docker', 'create'])
  assert f'{bundle.container_volume}:/var/ride/runtime' in create
  assert f'{root}:/bundle:ro' in create
  assert materialized[0][0] == (Path('/bundle'), Path('/var/ride/runtime'), '/usr/local/bin/python')
  assert any(command[-2:] == ['touch', '/var/ride/runtime/.complete'] for command in calls)
  assert calls[-1] == ['docker', 'rm', '-f', 'container-id']


def test_session_command_declaration_must_match_the_distributions_console_script(
  monkeypatch,
):
  output = json.dumps(
    [
      {
        'distribution': 'demo',
        'console': [['summon', 'demo.cli:main']],
        'declared': [['summon', 'demo.cli:other']],
      }
    ]
  )
  monkeypatch.setattr(
    runtime_bundle,
    '_run',
    lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ''),
  )

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='does not match'):
    runtime_bundle._session_commands(Path(sys.executable))


def test_host_session_environment_scrubs_launcher_activation(monkeypatch, tmp_path):
  root = tmp_path / 'bundle'
  bundle = runtime_bundle.RuntimeBundle(root, '3.12')
  launcher = tmp_path / 'launcher'
  monkeypatch.setenv('VIRTUAL_ENV', str(launcher))
  monkeypatch.setenv('PATH', os.pathsep.join([str(launcher / 'bin'), '/usr/local/bin', '/usr/bin']))
  monkeypatch.setenv('PYTHONHOME', '/python')

  env = bundle.host_session_env()

  assert 'VIRTUAL_ENV' not in env
  assert env['PATH'].split(os.pathsep) == [
    str(bundle.host_bin),
    '/usr/local/bin',
    '/usr/bin',
  ]
  assert 'PYTHONHOME' not in env


def test_resolver_holds_the_bundle_lock(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_classify_installation', lambda: ('3.12', ['a==1'], []))

  with runtime_bundle.resolve_runtime_bundle() as bundle:
    with (bundle.root / '.lock').open('a+') as handle:
      with pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_clean_removes_unlocked_bundles_and_keeps_locked_ones(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_remove_container_volume', lambda *_a, **_k: True)
  runtime = tmp_path / 'runtime'
  unlocked = runtime / ('a' * 64)
  locked = runtime / ('b' * 64)
  unlocked.mkdir(parents=True)
  locked.mkdir()

  with (locked / '.lock').open('a+') as handle:
    fcntl.flock(handle, fcntl.LOCK_SH)
    assert runtime_bundle.clean_runtime_bundles() == (1, 1)

  assert not unlocked.exists()
  assert locked.exists()


def test_clean_keeps_a_bundle_when_its_runtime_volume_is_in_use(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_remove_container_volume', lambda *_a, **_k: False)
  root = tmp_path / 'runtime' / ('a' * 64)
  root.mkdir(parents=True)

  assert runtime_bundle.clean_runtime_bundles() == (0, 1)
  assert root.is_dir()


def test_installed_distributions_publish_the_session_command_roster():
  commands = runtime_bundle._session_commands(Path(sys.executable))
  assert commands == [
    'bro',
    'bro.dev.git-golc',
    'broker',
    'broxy',
    'commit-footer',
    'credentials',
    'fold-branch',
    'land-pr',
    'mcp-server',
    'poll-pr',
    'rewind',
    'ride',
    'summon',
    'trails-server',
    'usage',
  ]
