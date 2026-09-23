import errno
import fcntl
import importlib.metadata
import io
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


_GIVEN_COMMANDS = ['ride', 'do-ride', 'summon']


def _materialized_runtime(root: Path) -> Path:
  venv_bin = root / 'venv' / 'bin'
  venv_bin.mkdir(parents=True)
  shim_directory = root / 'bin'
  shim_directory.mkdir()
  (venv_bin / 'python').symlink_to(sys.executable)
  for command in _GIVEN_COMMANDS:
    source = shutil.which(command)
    assert source is not None
    executable = venv_bin / command
    executable.symlink_to(source)
    (shim_directory / command).symlink_to(executable)
  return root


_PROBE_PYPROJECT = """\
[project]
name = "demo"
version = "1.0"

[build-system]
requires = ["uv_build>=0.9,<0.99"]
build-backend = "uv_build"
"""


_SETUPTOOLS_PYPROJECT = """\
[project]
name = "demo"
version = "1.0"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
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


def _stamped_wheel(
  path: Path, date_time: tuple[int, int, int, int, int, int], *, reverse: bool
) -> None:
  entries = {
    'demo/__init__.py': ('value = 1\n', 0o644),
    'demo/run.sh': ('exit 0\n', 0o755),
    'demo-1.0.dist-info/METADATA': ('Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n', 0o644),
    'demo-1.0.dist-info/WHEEL': (
      'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
      0o644,
    ),
  }
  with zipfile.ZipFile(path, 'w') as archive:
    for name in sorted(entries, reverse=reverse):
      content, mode = entries[name]
      entry = zipfile.ZipInfo(name, date_time)
      entry.external_attr = mode << 16
      archive.writestr(entry, content)


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


def test_a_git_installation_materializes_from_its_pin(monkeypatch, probe, tmp_path, caplog):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  _read_installation(monkeypatch, probe.site_packages['git'])

  with runtime_bundle.resolve_runtime_bundle() as bundle:
    with caplog.at_level('INFO'):
      bundle.materialize_host()
      bundle.materialize_host()

    assert caplog.text.count(f'materializing runtime bundle {bundle.hash[:12]}') == 1
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


def test_a_stamped_or_reordered_rebuild_freezes_one_bundle(monkeypatch, tmp_path):
  builds = iter([((2026, 8, 23, 23, 57, 12), False), ((2026, 8, 23, 23, 57, 14), True)])

  def build(command, *, description):
    del description
    out_dir = Path(command[command.index('--out-dir') + 1])
    date_time, reverse = next(builds)
    _stamped_wheel(out_dir / 'demo-1.0-py3-none-any.whl', date_time, reverse=reverse)
    return subprocess.CompletedProcess(command, 0, '', '')

  monkeypatch.setattr(runtime_bundle, '_run', build)
  local = [runtime_bundle._LocalDistribution('demo', tmp_path / 'source')]
  frozen = []
  for attempt in ('first', 'second'):
    staging = tmp_path / attempt
    staging.mkdir()
    wheels = runtime_bundle._build_wheels(local, staging)
    manifest = runtime_bundle._manifest('3.12', [], wheels)
    frozen.append(runtime_bundle._persist_bundle(tmp_path / 'base', manifest, wheels))

  assert frozen[0] == frozen[1]
  with zipfile.ZipFile(frozen[0] / 'wheels' / 'demo-1.0-py3-none-any.whl') as carried:
    assert carried.namelist() == [
      'demo/__init__.py',
      'demo/run.sh',
      'demo-1.0.dist-info/METADATA',
      'demo-1.0.dist-info/WHEEL',
    ]
    assert carried.read('demo/__init__.py') == b'value = 1\n'
    assert carried.getinfo('demo/run.sh').external_attr >> 16 == 0o755
    assert len({info.date_time for info in carried.infolist()}) == 1


def test_a_setuptools_wheel_leaves_out_a_stale_build_tree(tmp_path):
  source = tmp_path / 'source'
  (source / 'src' / 'demo').mkdir(parents=True)
  (source / 'pyproject.toml').write_text(_SETUPTOOLS_PYPROJECT)
  (source / 'src' / 'demo' / '__init__.py').touch()
  (source / 'build' / 'lib' / 'stale').mkdir(parents=True)
  (source / 'build' / 'lib' / 'stale' / '__init__.py').touch()
  staging = tmp_path / 'staging'
  staging.mkdir()

  [wheel] = runtime_bundle._build_wheels(
    [runtime_bundle._LocalDistribution('demo', source)], staging
  )

  with zipfile.ZipFile(wheel) as built:
    entries = built.namelist()
  assert 'demo/__init__.py' in entries
  assert [entry for entry in entries if entry.startswith('stale/')] == []


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
  assert not Path(os.readlink(host / 'bin' / 'summon')).is_absolute()


class _FakeMaterializerProcess:
  def __init__(self, command, **_kwargs):
    Path(command[command.index('--cidfile') + 1]).write_text('container-id\n')
    self.stdout = io.StringIO('ready\n')
    self.returncode = 0

  def __enter__(self):
    return self

  def __exit__(self, *_exception):
    return None


def test_container_materialization_populates_a_named_volume_once(monkeypatch, tmp_path, caplog):
  root = tmp_path / ('a' * 64)
  (root / 'wheels').mkdir(parents=True)
  bundle = runtime_bundle.RuntimeBundle(root, '3.12')
  calls: list[list[str]] = []
  launches: list[list[str]] = []
  materialized: list[tuple] = []

  def run(command, *args, **kwargs):
    del args, kwargs
    calls.append(command)
    if command[:5] == ['docker', 'exec', 'container-id', 'test', '-f']:
      return subprocess.CompletedProcess(command, 0 if len(materialized) > 0 else 1, '', '')
    return subprocess.CompletedProcess(command, 0, '', '')

  def launch(command, **kwargs):
    launches.append(command)
    return _FakeMaterializerProcess(command, **kwargs)

  monkeypatch.setattr(runtime_bundle.subprocess, 'run', run)
  monkeypatch.setattr(runtime_bundle.subprocess, 'Popen', launch)
  monkeypatch.setattr(
    runtime_bundle,
    '_materialize',
    lambda *args, **kwargs: materialized.append((args, kwargs)),
  )

  with caplog.at_level('INFO'):
    bundle.materialize_container('runtime-image')
    bundle.materialize_container('runtime-image')

  assert len(materialized) == 1
  assert caplog.text.count(f'materializing runtime bundle {bundle.hash[:12]}') == 1
  assert calls[0][:3] == ['docker', 'volume', 'create']
  assert len(launches) == 2
  create = launches[0]
  assert create[:4] == ['docker', 'run', '--rm', '--interactive']
  assert f'ride-materializer={bundle.hash}' in create
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


def test_unboxed_session_environment_is_a_closed_snapshot(monkeypatch, tmp_path):
  root = tmp_path / 'bundle'
  bundle = runtime_bundle.RuntimeBundle(root, '3.12')
  launcher = tmp_path / 'launcher'
  tree = tmp_path / 'tree'
  monkeypatch.setattr(
    runtime_bundle.os,
    'environ',
    {
      'VIRTUAL_ENV': str(launcher),
      'PATH': os.pathsep.join([str(launcher / 'bin'), '/usr/local/bin', '/usr/bin']),
      'HOME': '/home/operator',
      'LANG': 'en_US.UTF-8',
      'LC_TIME': 'C',
      'TERM': 'xterm-kitty',
      'RIDE_SUMMONED': '1',
      'CLAUDE_CONFIG_DIR': '/parent/claude',
      'PYTHONHOME': '/python',
      'SSL_CERT_FILE': '/etc/ssl/operator.pem',
    },
  )

  env = bundle.host_session_env(tree, tty=True)

  assert env == {
    'HOME': '/home/operator',
    'LANG': 'en_US.UTF-8',
    'LC_TIME': 'C',
    'TERM': 'xterm-kitty',
    'PATH': os.pathsep.join([str(bundle.host_bin), '/usr/local/bin', '/usr/bin']),
    'PWD': str(tree),
  }
  headless = bundle.host_session_env(tree, tty=False)
  assert 'TERM' not in headless
  assert headless['PWD'] == str(tree)
  added = bundle.host_session_env(
    tree,
    tty=True,
    additions={'IS_SANDBOX': '1', 'LANG': 'de_DE.UTF-8', 'PATH': '/x', 'TERM': 'forged'},
  )
  assert added == {**env, 'IS_SANDBOX': '1'}


def test_resolver_holds_the_bundle_lock(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_classify_installation', lambda: ('3.12', ['a==1'], []))

  with runtime_bundle.resolve_runtime_bundle() as bundle:
    with (bundle.root / '.lock').open('a+') as handle:
      with pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_hash_resolver_holds_the_existing_bundle_lock(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  manifest = runtime_bundle._manifest('3.12', [], [])
  root = runtime_bundle._persist_bundle(tmp_path, manifest, [])

  with runtime_bundle.resolve_runtime_bundle(root.name) as bundle:
    assert bundle.root == root
    with (root / '.lock').open('a+') as handle:
      with pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_the_bundle_hold_survives_the_holders_exec(tmp_path):
  data_home = tmp_path / 'data'
  manifest = runtime_bundle._manifest('3.12', [], [])
  root = runtime_bundle._persist_bundle(data_home / 'ride', manifest, [])
  waiter = 'import sys; print("held", flush=True); sys.stdin.readline()'
  holder = (
    'import os, sys\n'
    'from ride.runtime_bundle import resolve_runtime_bundle\n'
    f'with resolve_runtime_bundle({root.name!r}):\n'
    f'  os.execv(sys.executable, [sys.executable, "-c", {waiter!r}])\n'
  )

  with subprocess.Popen(
    [sys.executable, '-c', holder],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
    env={**os.environ, 'XDG_DATA_HOME': str(data_home)},
  ) as process:
    assert process.stdin is not None and process.stdout is not None
    assert process.stdout.readline() == 'held\n'
    with (root / '.lock').open('a+') as handle:
      with pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      process.stdin.close()
      assert process.wait(timeout=60) == 0
      fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_freeze_from_inside_a_frozen_bundle_resolves_that_bundle(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_classify_installation', lambda: ('3.12', ['a==1'], []))
  manifest = runtime_bundle._manifest('3.12', ['a==1'], [])
  root = runtime_bundle._persist_bundle(tmp_path, manifest, [])
  (root / 'host' / 'venv').mkdir(parents=True)
  monkeypatch.setattr(runtime_bundle.sys, 'prefix', str(root / 'host' / 'venv'))

  with runtime_bundle.resolve_runtime_bundle() as bundle:
    assert bundle.root == root


def test_a_freeze_from_inside_a_frozen_bundle_must_reproduce_it(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_classify_installation', lambda: ('3.12', ['a==2'], []))
  running = tmp_path / 'runtime' / ('b' * 64) / 'host' / 'venv'
  running.mkdir(parents=True)
  monkeypatch.setattr(runtime_bundle.sys, 'prefix', str(running))

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='must reproduce itself'):
    with runtime_bundle.resolve_runtime_bundle():
      pass
  assert [path.name for path in (tmp_path / 'runtime').iterdir()] == ['b' * 64]


def _without_docker(monkeypatch):
  monkeypatch.setattr(
    runtime_bundle.subprocess,
    'run',
    lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, '', 'no daemon'),
  )


def test_clean_removes_unlocked_bundles_and_keeps_locked_ones(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  monkeypatch.setattr(runtime_bundle, '_remove_container_volume', lambda *_a, **_k: True)
  _without_docker(monkeypatch)
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
  _without_docker(monkeypatch)
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
  volume_commands = [command for command in commands if command[:2] == ['docker', 'volume']]
  assert volume_commands[:2] == [
    ['docker', 'volume', 'inspect', f'ride-runtime-{bundle_hash}'],
    ['docker', 'volume', 'rm', f'ride-runtime-{bundle_hash}'],
  ]
  assert not root.exists()


def test_clean_removes_leaked_materializers_and_orphaned_volumes(monkeypatch, tmp_path):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  kept_hash = 'a' * 64
  gone_hash = 'b' * 64
  orphan_hash = 'c' * 64
  root = tmp_path / 'runtime' / kept_hash
  root.mkdir(parents=True)
  commands = []

  def run(command, **_kwargs):
    commands.append(command)
    if command[:2] == ['docker', 'ps']:
      output = f'held-leak {kept_hash}\nfree-leak {gone_hash}\nunlabeled \n'
      return subprocess.CompletedProcess(command, 0, output, '')
    if command[:3] == ['docker', 'volume', 'ls']:
      output = f'ride-runtime-{kept_hash}\nride-runtime-{orphan_hash}\nunrelated\n'
      return subprocess.CompletedProcess(command, 0, output, '')
    return subprocess.CompletedProcess(command, 0, '', '')

  monkeypatch.setattr(runtime_bundle.subprocess, 'run', run)

  with (root / '.lock').open('a+') as handle:
    fcntl.flock(handle, fcntl.LOCK_SH)
    assert runtime_bundle.clean_runtime_bundles() == (0, 1)

  removals = [command for command in commands if command[:3] == ['docker', 'rm', '-f']]
  assert removals == [['docker', 'rm', '-f', 'free-leak']]
  volume_removals = [command for command in commands if command[:3] == ['docker', 'volume', 'rm']]
  assert volume_removals == [['docker', 'volume', 'rm', f'ride-runtime-{orphan_hash}']]


def test_clean_removes_a_cleaned_bundles_leaked_materializer_before_its_volume(
  monkeypatch, tmp_path
):
  monkeypatch.setattr(runtime_bundle, 'runtime_base', lambda: tmp_path)
  bundle_hash = 'a' * 64
  root = tmp_path / 'runtime' / bundle_hash
  root.mkdir(parents=True)
  commands = []

  def run(command, **_kwargs):
    commands.append(command)
    if command[:2] == ['docker', 'ps']:
      return subprocess.CompletedProcess(command, 0, f'leak {bundle_hash}\n', '')
    return subprocess.CompletedProcess(command, 0, '', '')

  monkeypatch.setattr(runtime_bundle.subprocess, 'run', run)

  assert runtime_bundle.clean_runtime_bundles() == (1, 0)
  container_removal = commands.index(['docker', 'rm', '-f', 'leak'])
  volume_removal = commands.index(['docker', 'volume', 'rm', f'ride-runtime-{bundle_hash}'])
  assert container_removal < volume_removal
  assert not root.exists()


def test_installed_distributions_publish_the_session_command_roster():
  commands = runtime_bundle._session_commands(Path(sys.executable))
  assert commands == [
    'artifact',
    'benchmark-job',
    'benchmark-run',
    'bro',
    'bro.dev.git-golc',
    'broker',
    'broxy',
    'commit-footer',
    'credentials',
    'do-ride',
    'fold-branch',
    'land-pr',
    'mcp-server',
    'mission',
    'poll-pr',
    'pr-state',
    'quest',
    'rewind',
    'ride',
    'summon',
    'trails-server',
    'usage',
    'watch-next',
    'watch-run',
    'webview',
  ]


def test_given_runtime_is_taken_as_an_already_materialized_layout(tmp_path, monkeypatch):
  root = _materialized_runtime(tmp_path / 'given')
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python: _GIVEN_COMMANDS)
  monkeypatch.setattr(
    runtime_bundle,
    '_classify_installation',
    lambda: pytest.fail('a given runtime must not freeze the invoking installation'),
  )
  with runtime_bundle.resolve_runtime_bundle(str(root)) as bundle:
    assert bundle.host_venv == root / 'venv'
    assert bundle.host_bin == root / 'bin'
    assert bundle.reference == str(root)
    bundle.materialize_host()
    with pytest.raises(runtime_bundle.RuntimeBundleError, match='no frozen manifest'):
      bundle.require_frozen_manifest()


def test_given_runtime_refuses_an_incomplete_session_command_shim_farm(tmp_path, monkeypatch):
  root = _materialized_runtime(tmp_path / 'given')
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python: _GIVEN_COMMANDS)
  (root / 'bin' / 'summon').unlink()
  with pytest.raises(
    runtime_bundle.RuntimeBundleError, match='does not match its session commands'
  ):
    with runtime_bundle.resolve_runtime_bundle(str(root)):
      pass


def test_given_runtime_requires_the_materialized_layout(tmp_path):
  root = tmp_path / 'given'
  root.mkdir()
  with pytest.raises(runtime_bundle.RuntimeBundleError, match='missing directory'):
    with runtime_bundle.resolve_runtime_bundle(str(root)):
      pass


def test_a_given_runtime_session_trusts_the_roots_the_runtime_carries(tmp_path, monkeypatch):
  root = _materialized_runtime(tmp_path / 'given')
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python: _GIVEN_COMMANDS)
  roots = root / 'venv' / 'lib' / 'python3.12' / 'site-packages' / 'certifi' / 'cacert.pem'
  monkeypatch.setattr(runtime_bundle.certifi, 'where', lambda: str(roots))
  monkeypatch.setattr(
    runtime_bundle.os,
    'environ',
    {'HOME': '/root', 'PATH': '/usr/bin', 'SSL_CERT_FILE': '/etc/ssl/ambient.pem'},
  )
  bundle = runtime_bundle.RuntimeBundle(root, '3.12', materialized=True)

  env = bundle.host_session_env(tmp_path / 'tree', tty=False)

  assert env['SSL_CERT_FILE'] == str(roots)


def test_a_given_runtime_refuses_roots_outside_its_venv(tmp_path, monkeypatch):
  root = _materialized_runtime(tmp_path / 'given')
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python: _GIVEN_COMMANDS)
  monkeypatch.setattr(
    runtime_bundle.certifi, 'where', lambda: '/another/venv/site-packages/certifi/cacert.pem'
  )
  bundle = runtime_bundle.RuntimeBundle(root, '3.12', materialized=True)

  with pytest.raises(runtime_bundle.RuntimeBundleError, match='outside the materialized runtime'):
    bundle.host_session_env(tmp_path / 'tree', tty=False)


def test_runtime_reexec_uses_the_given_runtime_ride(tmp_path, monkeypatch):
  root = _materialized_runtime(tmp_path / 'given')
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python: _GIVEN_COMMANDS)
  monkeypatch.setattr(runtime_bundle.sys, 'prefix', '/another/venv')
  monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'shadow'))
  monkeypatch.setenv('PYTHONHOME', str(tmp_path / 'home'))
  monkeypatch.setenv('RIDE_PROBE', 'kept')
  calls = []
  monkeypatch.setattr(
    runtime_bundle.os,
    'execve',
    lambda executable, argv, env: calls.append((executable, argv, env)),
  )
  runtime_bundle.reexec_from_runtime(str(root), ['ride', 'solo', 'dev', 'work'])
  executable = str(root / 'venv' / 'bin' / 'ride')
  [(called, argv, env)] = calls
  assert (called, argv) == (executable, [executable, 'solo', 'dev', 'work'])
  assert env['RIDE_PROBE'] == 'kept'
  assert 'PYTHONPATH' not in env and 'PYTHONHOME' not in env


def test_the_reexeced_ride_ignores_an_ambient_shadow(tmp_path):
  root = _materialized_runtime(tmp_path / 'given')
  shadow = tmp_path / 'shadow' / 'ride'
  shadow.mkdir(parents=True)
  (shadow / '__init__.py').write_text('raise SystemExit("shadow ride imported")\n')
  launcher = (
    'import os\n'
    'import ride.runtime_bundle as runtime_bundle\n'
    f'runtime_bundle._session_commands = lambda _python: {_GIVEN_COMMANDS!r}\n'
    f'os.environ["PYTHONPATH"] = {str(shadow.parent)!r}\n'
    f'runtime_bundle.reexec_from_runtime({str(root)!r}, ["ride", "--help"])\n'
  )

  result = subprocess.run(
    [sys.executable, '-c', launcher],
    capture_output=True,
    text=True,
    env={key: value for key, value in os.environ.items() if key != 'PYTHONPATH'},
  )

  assert result.returncode == 0, result.stderr
  assert 'shadow ride imported' not in result.stderr
  assert result.stdout.startswith('usage: ride')


def test_runtime_reexec_is_a_noop_inside_the_named_runtime(tmp_path, monkeypatch):
  root = _materialized_runtime(tmp_path / 'given')
  monkeypatch.setattr(runtime_bundle, '_session_commands', lambda _python: _GIVEN_COMMANDS)
  monkeypatch.setattr(runtime_bundle.sys, 'prefix', str(root / 'venv'))
  monkeypatch.setattr(
    runtime_bundle.os,
    'execve',
    lambda *_args: pytest.fail('the runtime must not re-exec itself again'),
  )
  runtime_bundle.reexec_from_runtime(str(root), ['ride', 'list'])
