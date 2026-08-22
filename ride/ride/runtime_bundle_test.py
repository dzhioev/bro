import errno
import fcntl
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import ride.runtime_bundle as runtime_bundle

_discover_distributions = importlib.metadata.distributions

_PROBE_PYPROJECT = """\
[project]
name = "demo"
version = "1.0"

[build-system]
requires = ["uv_build>=0.9,<0.99"]
build-backend = "uv_build"
"""


class _Distribution:
  def __init__(
    self,
    name: str,
    version: str,
    direct_url: dict | str | None = None,
    *,
    egg_info: bool = False,
  ):
    self.metadata = {'Name': name}
    self.version = version
    self._direct_url = direct_url
    self._egg_info = egg_info

  def read_text(self, filename: str):
    if filename == 'METADATA':
      return None if self._egg_info else f'Name: {self.metadata["Name"]}\n'
    assert filename == 'direct_url.json'
    if self._direct_url is None or isinstance(self._direct_url, str):
      return self._direct_url
    return json.dumps(self._direct_url)


@dataclass(frozen=True)
class _ProbeInstallation:
  """one trivial distribution, installed by real `uv` every way an installation can arise."""

  source: Path
  wheel: Path
  commit: str
  site_packages: dict[str, Path]


def _uv(*arguments: str) -> None:
  subprocess.run(['uv', *arguments], check=True, capture_output=True)


@pytest.fixture(scope='module')
def probe(tmp_path_factory) -> _ProbeInstallation:
  root = tmp_path_factory.mktemp('provenance')
  source = root / 'source'
  (source / 'src' / 'demo').mkdir(parents=True)
  (source / 'pyproject.toml').write_text(_PROBE_PYPROJECT)
  (source / 'src' / 'demo' / '__init__.py').touch()
  _uv('build', '--wheel', '--out-dir', str(root / 'dist'), str(source))
  git = ['git', '-C', str(source), '-c', 'user.email=probe@invalid', '-c', 'user.name=probe']
  subprocess.run([*git, 'init', '--quiet'], check=True, capture_output=True)
  subprocess.run([*git, 'add', '--all'], check=True, capture_output=True)
  subprocess.run([*git, 'commit', '--quiet', '--message', 'probe'], check=True, capture_output=True)
  commit = subprocess.run(
    [*git, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
  ).stdout.strip()
  wheel = next((root / 'dist').glob('*.whl'))
  installations = {
    'index': ['--no-index', '--find-links', str(root / 'dist'), 'demo==1.0'],
    'directory': [str(source)],
    'editable': ['--editable', str(source)],
    'git': [f'git+{source.as_uri()}'],
    'wheel': [str(wheel)],
  }
  site_packages = {}
  for kind, arguments in installations.items():
    venv = root / f'venv-{kind}'
    _uv('venv', '--python', sys.executable, str(venv))
    _uv('pip', 'install', '--python', str(venv / 'bin' / 'python'), *arguments)
    site_packages[kind] = _site_packages(venv)
  return _ProbeInstallation(source, wheel, commit, site_packages)


def _site_packages(venv: Path) -> Path:
  return next((venv / 'lib').glob('python*/site-packages'))


def _read_installation(monkeypatch, site_packages: Path) -> None:
  monkeypatch.setattr(
    runtime_bundle.importlib.metadata,
    'distributions',
    lambda: _discover_distributions(path=[str(site_packages)]),
  )


def _classify(
  monkeypatch, site_packages: Path
) -> tuple[list[str], list[runtime_bundle._LocalDistribution]]:
  _read_installation(monkeypatch, site_packages)
  _python, pins, local = runtime_bundle._classify_installation()
  return pins, local


def _pure_wheel(path: Path) -> None:
  with zipfile.ZipFile(path, 'w') as archive:
    archive.writestr(
      'demo-1.0.dist-info/WHEEL',
      'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    )


def test_classifies_every_supported_provenance(monkeypatch, tmp_path):
  directory = tmp_path / 'source'
  (directory / 'nested').mkdir(parents=True)
  wheel = tmp_path / 'carried-1.0-py3-none-any.whl'
  wheel.touch()
  distributions = [
    _Distribution('Index-Package', '2.0'),
    _Distribution(
      'Editable-Package', '1.0', {'url': directory.as_uri(), 'dir_info': {'editable': True}}
    ),
    _Distribution('Directory-Package', '1.0', {'url': directory.as_uri(), 'dir_info': {}}),
    _Distribution(
      'Nested-Package', '1.0', {'url': directory.as_uri(), 'dir_info': {}, 'subdirectory': 'nested'}
    ),
    _Distribution('Wheel-Package', '1.0', {'url': wheel.as_uri(), 'archive_info': {}}),
    _Distribution(
      'Git-Package',
      '1.0',
      {
        'url': 'https://github.com/dzhioev/bro.git',
        'vcs_info': {'vcs': 'git', 'commit_id': 'c0ffee', 'requested_revision': 'master'},
        'subdirectory': 'ride',
      },
    ),
    _Distribution(
      'Mercurial-Package',
      '1.0',
      {'url': 'https://example.invalid/repo', 'vcs_info': {'vcs': 'hg', 'commit_id': 'beef'}},
    ),
    _Distribution(
      'Download-Package',
      '1.0',
      {'url': 'https://example.invalid/download.whl', 'archive_info': {'hashes': {'sha256': 'ab'}}},
    ),
    _Distribution(
      'Unhashed-Package', '1.0', {'url': 'https://example.invalid/plain.whl', 'archive_info': {}}
    ),
  ]
  monkeypatch.setattr(runtime_bundle.importlib.metadata, 'distributions', lambda: distributions)

  python, pins, local = runtime_bundle._classify_installation()

  assert python == f'{sys.version_info.major}.{sys.version_info.minor}'
  assert pins == [
    'Download-Package @ https://example.invalid/download.whl#sha256=ab',
    'Git-Package @ git+https://github.com/dzhioev/bro.git@c0ffee#subdirectory=ride',
    'Index-Package==2.0',
    'Mercurial-Package @ hg+https://example.invalid/repo@beef',
    'Unhashed-Package @ https://example.invalid/plain.whl',
  ]
  assert local == [
    runtime_bundle._LocalDistribution('Directory-Package', directory),
    runtime_bundle._LocalDistribution('Editable-Package', directory),
    runtime_bundle._LocalDistribution('Nested-Package', directory / 'nested'),
    runtime_bundle._LocalDistribution('Wheel-Package', wheel),
  ]


@pytest.mark.parametrize(
  ('direct_url', 'message'),
  [
    ('{', 'malformed direct_url.json'),
    ({'dir_info': {}}, 'has no URL'),
    ({'url': 'https://example.invalid/x.whl'}, 'records no installation source'),
    (
      {'url': 'https://example.invalid/repo', 'vcs_info': {'commit_id': 'beef'}},
      'no version control',
    ),
    ({'url': 'https://example.invalid/repo', 'vcs_info': {'vcs': 'git'}}, 'no resolved git commit'),
    ({'url': 'https://example.invalid/src', 'dir_info': {}}, 'unsupported directory installation'),
    ({'url': 'file:///absent/source', 'dir_info': {}}, 'is not a directory'),
    ({'url': 'file:///absent/package.whl', 'archive_info': {}}, 'archive is missing'),
    (
      {'url': 'file:///absent/package.tar.gz', 'archive_info': {}, 'subdirectory': 'inner'},
      'reinstall it from that source directory',
    ),
  ],
)
def test_refuses_an_unreproducible_installation(monkeypatch, direct_url, message):
  monkeypatch.setattr(
    runtime_bundle.importlib.metadata,
    'distributions',
    lambda: [_Distribution('demo', '1.0', direct_url)],
  )

  with pytest.raises(runtime_bundle.RuntimeBundleError, match=message):
    runtime_bundle._classify_installation()


@pytest.mark.parametrize('egg_info_first', [False, True])
def test_a_source_tree_egg_info_does_not_shadow_its_installation(
  monkeypatch, tmp_path, egg_info_first
):
  directory = tmp_path / 'source'
  directory.mkdir()
  records = [
    _Distribution('demo', '1.0', {'url': directory.as_uri(), 'dir_info': {'editable': True}}),
    _Distribution('demo', '1.0', egg_info=True),
  ]
  if egg_info_first:
    records.reverse()
  monkeypatch.setattr(runtime_bundle.importlib.metadata, 'distributions', lambda: records)

  _python, pins, local = runtime_bundle._classify_installation()

  assert pins == []
  assert local == [runtime_bundle._LocalDistribution('demo', directory)]


def test_refuses_two_installations_of_one_distribution(monkeypatch):
  monkeypatch.setattr(
    runtime_bundle.importlib.metadata,
    'distributions',
    lambda: [_Distribution('Demo-Package', '1.0'), _Distribution('demo_package', '2.0')],
  )

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='duplicate installed distribution'):
    runtime_bundle._classify_installation()


def test_an_egg_info_beside_a_real_editable_installation_classifies_once(
  monkeypatch, probe, tmp_path
):
  egg_info = tmp_path / 'demo.egg-info'
  egg_info.mkdir()
  (egg_info / 'PKG-INFO').write_text('Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n')
  monkeypatch.setattr(
    runtime_bundle.importlib.metadata,
    'distributions',
    lambda: _discover_distributions(path=[str(probe.site_packages['editable']), str(tmp_path)]),
  )

  _python, pins, local = runtime_bundle._classify_installation()

  assert pins == []
  assert local == [runtime_bundle._LocalDistribution('demo', probe.source)]


def test_refuses_a_distribution_recorded_only_by_an_egg_info(monkeypatch, tmp_path):
  egg_info = tmp_path / 'demo.egg-info'
  egg_info.mkdir()
  (egg_info / 'PKG-INFO').write_text('Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n')
  monkeypatch.setattr(
    runtime_bundle.importlib.metadata,
    'distributions',
    lambda: _discover_distributions(path=[str(tmp_path)]),
  )

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='egg-info build artifact') as error:
    runtime_bundle._classify_installation()

  assert str(tmp_path) in str(error.value)


def test_a_real_installer_matrix_classifies_by_provenance(monkeypatch, probe):
  assert _classify(monkeypatch, probe.site_packages['index']) == (['demo==1.0'], [])
  assert _classify(monkeypatch, probe.site_packages['git']) == (
    [f'demo @ git+{probe.source.as_uri()}@{probe.commit}'],
    [],
  )
  carried = [runtime_bundle._LocalDistribution('demo', probe.source)]
  assert _classify(monkeypatch, probe.site_packages['directory']) == ([], carried)
  assert _classify(monkeypatch, probe.site_packages['editable']) == ([], carried)
  assert _classify(monkeypatch, probe.site_packages['wheel']) == (
    [],
    [runtime_bundle._LocalDistribution('demo', probe.wheel)],
  )


def test_a_git_installation_materializes_from_its_pin(monkeypatch, probe, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  _read_installation(monkeypatch, probe.site_packages['git'])

  with runtime_bundle.resolve_runtime_bundle() as bundle:
    bundle.materialize_host()

    assert (bundle.root / 'pins.txt').read_text() == (
      f'demo @ git+{probe.source.as_uri()}@{probe.commit}\n'
    )
    assert _classify(monkeypatch, _site_packages(bundle.host_venv))[0] == [
      f'demo @ git+{probe.source.as_uri()}@{probe.commit}'
    ]


def test_a_host_snapshot_reresolves_to_its_own_bundle(monkeypatch, probe, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  _read_installation(monkeypatch, probe.site_packages['directory'])

  with runtime_bundle.resolve_runtime_bundle() as bundle:
    bundle.materialize_host()
    _read_installation(monkeypatch, _site_packages(bundle.host_venv))

    with runtime_bundle.resolve_runtime_bundle() as snapshot:
      assert snapshot.hash == bundle.hash


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


def test_clean_removes_the_matching_runtime_volume_before_the_bundle(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  bundle_hash = 'a' * 64
  root = tmp_path / 'runtime' / bundle_hash
  root.mkdir(parents=True)
  commands = []

  def run(command, **_kwargs):
    commands.append(command)
    return subprocess.CompletedProcess(command, 0, '', '')

  monkeypatch.setattr(runtime_bundle.subprocess, 'run', run)

  assert runtime_bundle.clean_runtime_bundles() == (1, 0)
  assert commands == [
    ['docker', 'volume', 'inspect', f'ride-runtime-{bundle_hash}'],
    ['docker', 'volume', 'rm', f'ride-runtime-{bundle_hash}'],
  ]
  assert not root.exists()


def test_installed_distributions_publish_the_session_command_roster():
  commands = runtime_bundle._session_commands(Path(sys.executable))
  assert commands == [
    'benchmark-job',
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
