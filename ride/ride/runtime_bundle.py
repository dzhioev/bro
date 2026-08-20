import contextlib
import errno
import fcntl
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from email.message import Message
from email.parser import Parser
from pathlib import Path

from bro.workspace.paths import runtime_base

_SESSION_COMMAND_GROUP = 'bro.session_commands'
_HASH_PATTERN = re.compile(r'[0-9a-f]{64}')


class RuntimeBundleError(RuntimeError):
  """the ride installation cannot be frozen or materialized as a runtime bundle."""


@dataclass(frozen=True)
class _LocalDistribution:
  name: str
  source: Path


@dataclass(frozen=True)
class RuntimeBundle:
  root: Path
  python_version: str

  @property
  def host_venv(self) -> Path:
    return self.root / 'host' / 'venv'

  @property
  def host_bin(self) -> Path:
    return self.root / 'host' / 'bin'

  @property
  def hash(self) -> str:
    return self.root.name

  @property
  def container_volume(self) -> str:
    return f'ride-runtime-{self.hash}'

  def materialize_host(self) -> None:
    lock_path = self.root / '.materialize.lock'
    with _locked_file(lock_path, fcntl.LOCK_EX):
      host = self.root / 'host'
      complete = host / '.complete'
      if complete.is_file():
        return
      shutil.rmtree(host, ignore_errors=True)
      host.mkdir()
      _materialize(self.root, host, sys.executable)
      complete.touch()

  def materialize_container(self, image: str) -> None:
    with _locked_file(self.root / '.materialize.lock', fcntl.LOCK_EX):
      _run(
        ['docker', 'volume', 'create', self.container_volume],
        description='cannot create container runtime volume',
      )
      with _materializer_container(self, image) as container_id:
        complete = subprocess.run(
          ['docker', 'exec', container_id, 'test', '-f', '/var/ride/runtime/.complete'],
          capture_output=True,
        )
        if complete.returncode == 0:
          return
        _container_run(
          container_id,
          ['find', '/var/ride/runtime', '-mindepth', '1', '-delete'],
          description='cannot clear incomplete container runtime',
        )
        wheels = sorted((self.root / 'wheels').glob('*.whl'))
        wheel_names = [wheel.name for wheel in wheels]
        for wheel in wheels:
          _assert_pure_wheel(wheel)
        _materialize(
          Path('/bundle'),
          Path('/var/ride/runtime'),
          '/usr/local/bin/python',
          wheel_names=wheel_names,
          run=lambda command, description: _container_run(
            container_id, command, description=description
          ),
        )
        _container_run(
          container_id,
          ['touch', '/var/ride/runtime/.complete'],
          description='cannot mark container runtime complete',
        )

  def host_session_env(self) -> dict[str, str]:
    env = dict(os.environ)
    launcher_venv = env.pop('VIRTUAL_ENV', None)
    path_entries = env.get('PATH', '').split(os.pathsep)
    if launcher_venv is not None:
      launcher_bin = os.path.normpath(str(Path(launcher_venv) / 'bin'))
      path_entries = [entry for entry in path_entries if os.path.normpath(entry) != launcher_bin]
    env['PATH'] = os.pathsep.join(_unique_paths([str(self.host_bin), *path_entries]))
    env.pop('PYTHONHOME', None)
    return env


def _unique_paths(paths: Iterable[str]) -> list[str]:
  result: list[str] = []
  seen: set[str] = set()
  for path in paths:
    if len(path) == 0:
      continue
    normalized = os.path.normcase(os.path.normpath(path))
    if normalized in seen:
      continue
    seen.add(normalized)
    result.append(path)
  return result


@contextlib.contextmanager
def _locked_file(path: Path, operation: int) -> Generator[object]:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('a+') as handle:
    fcntl.flock(handle, operation)
    yield handle


def _canonical_name(name: str) -> str:
  return re.sub(r'[-_.]+', '-', name).lower()


def _local_source(distribution: importlib.metadata.Distribution, text: str) -> Path:
  try:
    direct_url = json.loads(text)
  except json.JSONDecodeError as error:
    raise RuntimeBundleError(
      f'{distribution.metadata["Name"]}: malformed direct_url.json: {error}'
    ) from error
  if not isinstance(direct_url, dict):
    raise RuntimeBundleError(f'{distribution.metadata["Name"]}: malformed direct_url.json')
  url = direct_url.get('url')
  if not isinstance(url, str):
    raise RuntimeBundleError(f'{distribution.metadata["Name"]}: direct_url.json has no URL')
  parsed = urllib.parse.urlparse(url)
  if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
    raise RuntimeBundleError(
      f'{distribution.metadata["Name"]}: unsupported direct installation URL {url!r}'
    )
  source = Path(urllib.request.url2pathname(urllib.parse.unquote(parsed.path)))
  if not source.is_dir():
    raise RuntimeBundleError(
      f'{distribution.metadata["Name"]}: local installation source is not a directory: {source}'
    )
  return source.resolve()


def _classify_installation() -> tuple[str, list[str], list[_LocalDistribution]]:
  python = f'{sys.version_info.major}.{sys.version_info.minor}'
  pins: list[str] = []
  local: list[_LocalDistribution] = []
  seen: dict[str, str] = {}
  for distribution in importlib.metadata.distributions():
    name = distribution.metadata['Name']
    if not isinstance(name, str) or len(name) == 0:
      raise RuntimeBundleError('installed distribution has no name')
    canonical = _canonical_name(name)
    if canonical in seen:
      raise RuntimeBundleError(
        f'duplicate installed distribution {name!r} (also provided as {seen[canonical]!r})'
      )
    seen[canonical] = name
    direct_url = distribution.read_text('direct_url.json')
    if direct_url is None:
      version = distribution.version
      if len(version) == 0:
        raise RuntimeBundleError(f'{name}: installed distribution has no version')
      pins.append(f'{name}=={version}')
    else:
      local.append(_LocalDistribution(name, _local_source(distribution, direct_url)))
  pins.sort(key=str.casefold)
  local.sort(key=lambda item: item.name.casefold())
  return python, pins, local


def _run(command: list[str], *, description: str) -> subprocess.CompletedProcess[str]:
  try:
    result = subprocess.run(command, capture_output=True, text=True)
  except OSError as error:
    raise RuntimeBundleError(f'{description}: {error}') from error
  if result.returncode == 0:
    return result
  detail = result.stderr.strip() or result.stdout.strip() or f'exit code {result.returncode}'
  raise RuntimeBundleError(f'{description}: {detail}')


def _container_run(
  container_id: str, command: list[str], *, description: str
) -> subprocess.CompletedProcess[str]:
  return _run(['docker', 'exec', container_id, *command], description=description)


@contextlib.contextmanager
def _materializer_container(bundle: RuntimeBundle, image: str) -> Generator[str]:
  result = _run(
    [
      'docker',
      'create',
      '--entrypoint',
      'sleep',
      '-v',
      f'{bundle.container_volume}:/var/ride/runtime',
      '-v',
      f'{bundle.root}:/bundle:ro',
      image,
      'infinity',
    ],
    description='cannot create container runtime materializer',
  )
  container_id = result.stdout.strip()
  try:
    _run(
      ['docker', 'start', container_id], description='cannot start container runtime materializer'
    )
    yield container_id
  finally:
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)


def _wheel_record(path: Path, suffix: str) -> Message:
  try:
    with zipfile.ZipFile(path) as archive:
      records = [name for name in archive.namelist() if name.endswith(suffix)]
      if len(records) != 1:
        raise RuntimeBundleError(f'{path.name}: wheel has {len(records)} {suffix} records')
      return Parser().parsestr(archive.read(records[0]).decode())
  except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
    raise RuntimeBundleError(f'cannot inspect wheel {path}: {error}') from error


def _build_wheels(local: list[_LocalDistribution], wheels: Path) -> list[Path]:
  built: list[Path] = []
  names: set[str] = set()
  for distribution in local:
    output = wheels / _canonical_name(distribution.name)
    output.mkdir()
    _run(
      [
        'uv',
        'build',
        '--wheel',
        '--no-build-logs',
        '--out-dir',
        str(output),
        str(distribution.source),
      ],
      description=f'cannot build local distribution {distribution.name}',
    )
    candidates = list(output.glob('*.whl'))
    if len(candidates) != 1:
      raise RuntimeBundleError(
        f'{distribution.name}: uv build produced {len(candidates)} wheels, expected one'
      )
    wheel = candidates[0]
    built_name = _wheel_record(wheel, '.dist-info/METADATA').get('Name')
    if not isinstance(built_name, str) or _canonical_name(built_name) != _canonical_name(
      distribution.name
    ):
      raise RuntimeBundleError(f'{distribution.name}: uv build produced a wheel for {built_name!r}')
    if wheel.name in names:
      raise RuntimeBundleError(f'duplicate built wheel filename: {wheel.name}')
    names.add(wheel.name)
    built.append(wheel)
  return sorted(built, key=lambda path: path.name.casefold())


def _wheel_digest(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  return digest.hexdigest()


def _manifest(python: str, pins: list[str], wheels: list[Path]) -> dict:
  return {
    'python': python,
    'pins': pins,
    'wheels': {wheel.name: _wheel_digest(wheel) for wheel in wheels},
  }


def _manifest_bytes(manifest: dict) -> bytes:
  return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


@contextlib.contextmanager
def _staging_directory(parent: Path) -> Generator[Path]:
  path = Path(tempfile.mkdtemp(prefix='.building-', dir=parent))
  try:
    yield path
  finally:
    shutil.rmtree(path, ignore_errors=True)


def _persist_bundle(base: Path, manifest: dict, wheels: list[Path]) -> Path:
  runtime = base / 'runtime'
  runtime.mkdir(parents=True, exist_ok=True)
  digest = hashlib.sha256(_manifest_bytes(manifest)).hexdigest()
  target = runtime / digest
  if target.exists():
    _verify_bundle(target, manifest)
    return target
  with _staging_directory(runtime) as staging:
    (staging / 'wheels').mkdir()
    (staging / 'bundle.json').write_bytes(_manifest_bytes(manifest) + b'\n')
    (staging / 'pins.txt').write_text(''.join(f'{pin}\n' for pin in manifest['pins']))
    for wheel in wheels:
      shutil.copyfile(wheel, staging / 'wheels' / wheel.name)
    try:
      staging.rename(target)
    except OSError as error:
      if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
        raise
      _verify_bundle(target, manifest)
  return target


def _verify_bundle(root: Path, manifest: dict) -> None:
  try:
    stored = json.loads((root / 'bundle.json').read_text())
  except (FileNotFoundError, json.JSONDecodeError) as error:
    raise RuntimeBundleError(f'corrupt runtime bundle at {root}: {error}') from error
  expected_pins = ''.join(f'{pin}\n' for pin in manifest['pins'])
  try:
    stored_pins = (root / 'pins.txt').read_text()
  except FileNotFoundError as error:
    raise RuntimeBundleError(f'corrupt runtime bundle at {root}: {error}') from error
  wheel_root = root / 'wheels'
  wheel_names = {path.name for path in wheel_root.glob('*.whl')}
  if stored != manifest or stored_pins != expected_pins or wheel_names != manifest['wheels'].keys():
    raise RuntimeBundleError(f'content hash collision or corrupt runtime bundle at {root}')
  for filename, expected in manifest['wheels'].items():
    wheel = root / 'wheels' / filename
    if not wheel.is_file() or _wheel_digest(wheel) != expected:
      raise RuntimeBundleError(f'corrupt runtime bundle wheel: {wheel}')


def _assert_pure_wheel(path: Path) -> None:
  metadata = _wheel_record(path, '.dist-info/WHEEL')
  tags = metadata.get_all('Tag', [])
  if metadata.get('Root-Is-Purelib', '').lower() != 'true' or len(tags) == 0:
    raise RuntimeBundleError(f'{path.name}: local distribution did not build a pure-Python wheel')
  if any(tag.split('-')[-2:] != ['none', 'any'] for tag in tags):
    raise RuntimeBundleError(f'{path.name}: local distribution did not build a pure-Python wheel')


def _entry_point_map(distribution: str, group: str, entries: object) -> dict[str, str]:
  if not isinstance(entries, list):
    raise RuntimeBundleError(f'{distribution}: malformed {group} declarations')
  result: dict[str, str] = {}
  for entry in entries:
    if (
      not isinstance(entry, list)
      or len(entry) != 2
      or not isinstance(entry[0], str)
      or not isinstance(entry[1], str)
    ):
      raise RuntimeBundleError(f'{distribution}: malformed {group} declaration')
    name, value = entry
    if name in result:
      raise RuntimeBundleError(f'{distribution}: duplicate {group} declaration {name!r}')
    result[name] = value
  return result


def _session_commands(
  python: Path,
  *,
  run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
  command_runner = _run if run is None else run
  script = f'''\
import importlib.metadata
import json
result = []
for distribution in importlib.metadata.distributions():
  console = [[entry.name, entry.value] for entry in distribution.entry_points if entry.group == "console_scripts"]
  declared = [[entry.name, entry.value] for entry in distribution.entry_points if entry.group == "{_SESSION_COMMAND_GROUP}"]
  if declared:
    result.append({{"distribution": distribution.metadata["Name"], "console": console, "declared": declared}})
print(json.dumps(result))
'''
  result = command_runner(
    [str(python), '-c', script], description='cannot read session command declarations'
  )
  try:
    declarations = json.loads(result.stdout)
  except json.JSONDecodeError as error:
    raise RuntimeBundleError(f'cannot read session command declarations: {error}') from error
  if not isinstance(declarations, list):
    raise RuntimeBundleError('session command declarations are malformed')
  commands: list[str] = []
  owners: dict[str, str] = {}
  for declaration in declarations:
    if not isinstance(declaration, dict) or not isinstance(
      distribution := declaration.get('distribution'), str
    ):
      raise RuntimeBundleError('session command declaration is malformed')
    console = _entry_point_map(distribution, 'console script', declaration.get('console'))
    declared = _entry_point_map(distribution, _SESSION_COMMAND_GROUP, declaration.get('declared'))
    for command, value in declared.items():
      if Path(command).name != command or command in ('.', '..'):
        raise RuntimeBundleError(f'{distribution}: invalid session command name {command!r}')
      if console.get(command) != value:
        raise RuntimeBundleError(
          f'{distribution}: session command {command!r} does not match its console script'
        )
      if command in owners:
        raise RuntimeBundleError(
          f'session command {command!r} declared by both {owners[command]} and {distribution}'
        )
      owners[command] = distribution
      commands.append(command)
  return sorted(commands)


def _materialize(
  bundle: Path,
  target: Path,
  python: str,
  *,
  wheel_names: list[str] | None = None,
  run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
  command_runner = _run if run is None else run
  wheels = (
    sorted((bundle / 'wheels').glob('*.whl'))
    if wheel_names is None
    else [bundle / 'wheels' / name for name in wheel_names]
  )
  if wheel_names is None:
    for wheel in wheels:
      _assert_pure_wheel(wheel)
  venv = target / 'venv'
  command_runner(
    ['uv', 'venv', '--python', python, str(venv)], description='cannot create runtime venv'
  )
  install = [
    'uv',
    'pip',
    'install',
    '--python',
    str(venv / 'bin' / 'python'),
    '--no-deps',
    '-r',
    str(bundle / 'pins.txt'),
    *(str(wheel) for wheel in wheels),
  ]
  command_runner(install, description='cannot install runtime bundle')
  command_runner(
    ['uv', 'pip', 'check', '--python', str(venv / 'bin' / 'python')],
    description='runtime dependency closure is incomplete',
  )
  shim_dir = target / 'bin'
  command_runner(['mkdir', str(shim_dir)], description='cannot create runtime command directory')
  for command in _session_commands(venv / 'bin' / 'python', run=command_runner):
    command_target = venv / 'bin' / command
    command_runner(
      ['test', '-f', str(command_target)],
      description=f'session command has no materialized console script: {command_target}',
    )
    command_runner(
      ['ln', '-s', str(command_target), str(shim_dir / command)],
      description=f'cannot create session command shim {command}',
    )


@contextlib.contextmanager
def resolve_runtime_bundle() -> Generator[RuntimeBundle]:
  handle = None
  try:
    python, pins, local = _classify_installation()
    base = runtime_base()
    runtime = base / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ride-runtime-wheels-') as temporary:
      wheels = _build_wheels(local, Path(temporary))
      manifest = _manifest(python, pins, wheels)
      with _locked_file(runtime / '.lock', fcntl.LOCK_SH):
        root = _persist_bundle(base, manifest, wheels)
        handle = (root / '.lock').open('a+')
        fcntl.flock(handle, fcntl.LOCK_SH)
    yield RuntimeBundle(root, python)
  finally:
    if handle is not None:
      handle.close()


def _remove_container_volume(bundle_hash: str, *, dry_run: bool) -> bool:
  volume = f'ride-runtime-{bundle_hash}'
  try:
    present = subprocess.run(
      ['docker', 'volume', 'inspect', volume], capture_output=True, text=True
    )
  except FileNotFoundError:
    return True
  if present.returncode != 0:
    detail = present.stderr.lower()
    return 'no such volume' in detail or 'not found' in detail
  if dry_run:
    return True
  removed = subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True, text=True)
  return removed.returncode == 0


def clean_runtime_bundles(*, dry_run: bool = False) -> tuple[int, int]:
  runtime = runtime_base() / 'runtime'
  if not runtime.is_dir():
    return 0, 0
  removed = 0
  skipped = 0
  with _locked_file(runtime / '.lock', fcntl.LOCK_EX):
    for root in sorted(runtime.iterdir()):
      if not root.is_dir():
        continue
      if root.name.startswith('.building-'):
        if not dry_run:
          shutil.rmtree(root)
        continue
      if _HASH_PATTERN.fullmatch(root.name) is None:
        continue
      with (root / '.lock').open('a+') as handle:
        try:
          fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
          skipped += 1
          continue
        if not _remove_container_volume(root.name, dry_run=dry_run):
          skipped += 1
          continue
        if not dry_run:
          shutil.rmtree(root)
        removed += 1
  return removed, skipped
