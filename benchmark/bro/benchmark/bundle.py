#!/usr/bin/env python
"""build the relocatable directory a machine with no Python runs `bro` from.

The bundle is self-contained: a pinned standalone CPython, the framework
distributions a bro runs from resolved against the workspace lock, and a shim
that puts them together. Copying the directory anywhere on a linux/x86_64 glibc
machine is the whole installation — nothing outside it is read.

What the bundle contains is decided by the pinned interpreter version here plus
the workspace `uv.lock`, so two builds of one commit carry the same code.
"""

import contextlib
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import log, spawn
from bro.base.args import Parser
from bro.base.source_root import SOURCE_ROOT

__cli_name__ = 'benchmark-bundle'

CPYTHON_VERSION = '3.12.14'
# core, the engine that runs a bro, and the distribution every persona but the
# minimal `bro` one ships from — a bundle without it registers no other bro
WHEEL_PACKAGES = ('bro', 'bro-native', 'bro-dev')
TARGET = ('linux', 'x86_64', 'glibc')
MANIFEST_FORMAT = 2

_SHIM = """\
#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$root/{packages}${{PYTHONPATH:+:$PYTHONPATH}}"
export PYTHONPATH
exec "$root/{interpreter}" -s "$root/{command}" "$@"
"""


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
  return json.dumps(manifest, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()


def _digest_valid(value: object) -> bool:
  return (
    isinstance(value, str)
    and len(value) == 64
    and all(character in '0123456789abcdef' for character in value)
  )


def _load_manifest(path: Path) -> dict[str, object]:
  try:
    manifest = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError) as error:
    raise ValueError(f'invalid bundle manifest at {path}: {error}') from error
  if not isinstance(manifest, dict) or set(manifest) != {
    'format',
    'cpython',
    'requirements',
    'shim',
    'source_commit',
    'target',
    'wheels',
  }:
    raise ValueError(f'invalid bundle manifest at {path}: unexpected fields')
  target = manifest['target']
  wheels = manifest['wheels']
  if (
    manifest['format'] != MANIFEST_FORMAT
    or not isinstance(manifest['cpython'], str)
    or manifest['cpython'] == ''
    or not isinstance(manifest['requirements'], str)
    or manifest['requirements'] == ''
    or not _digest_valid(manifest['shim'])
    or not isinstance(manifest['source_commit'], str)
    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', manifest['source_commit'])
    or not isinstance(target, list)
    or len(target) == 0
    or not all(isinstance(part, str) and part != '' for part in target)
    or not isinstance(wheels, dict)
    or len(wheels) == 0
    or not all(
      isinstance(filename, str) and filename != '' and _digest_valid(digest)
      for filename, digest in wheels.items()
    )
  ):
    raise ValueError(f'invalid bundle manifest at {path}: malformed values')
  return manifest


@dataclass(frozen=True)
class Bundle:
  """a built bundle, addressed by the parts a consumer needs to name."""

  root: Path

  @property
  def shim(self) -> Path:
    return self.root / 'bro'

  @property
  def python(self) -> Path:
    return self.root / 'python'

  @property
  def interpreter(self) -> Path:
    return self.python / 'bin' / 'python3'

  @property
  def site_packages(self) -> Path:
    return self.root / 'site-packages'

  @property
  def command(self) -> Path:
    """the framework's own `bro` console script, which the shim runs.

    Reachable only that way: the installer wrote the build machine's interpreter
    path into its shebang, which dangles the moment the bundle is copied
    anywhere.
    """
    return self.site_packages / 'bin' / 'bro'

  @property
  def ca_bundle(self) -> Path:
    """the CA store to point `SSL_CERT_FILE` at, for a host that ships none."""
    return self.site_packages / 'certifi' / 'cacert.pem'

  @property
  def manifest(self) -> Path:
    return self.root / 'bundle.json'

  @property
  def identity(self) -> str:
    manifest = _load_manifest(self.manifest)
    return f'sha256:{hashlib.sha256(_manifest_bytes(manifest)).hexdigest()}'

  @property
  def source_commit(self) -> str:
    return str(_load_manifest(self.manifest)['source_commit'])

  def missing(self) -> tuple[Path, ...]:
    parts = (self.shim, self.interpreter, self.command, self.ca_bundle, self.manifest)
    return tuple(part for part in parts if not part.exists())


def shim_text(bundle: Bundle) -> str:
  """the launcher script, spelling the layout relative to wherever it ends up."""
  return _SHIM.format(
    packages=bundle.site_packages.relative_to(bundle.root),
    interpreter=bundle.interpreter.relative_to(bundle.root),
    command=bundle.command.relative_to(bundle.root),
  )


def _file_digest(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  return digest.hexdigest()


def _bundle_manifest(
  bundle: Bundle, requirements: str, wheels: list[Path], source_commit: str
) -> dict[str, object]:
  return {
    'format': MANIFEST_FORMAT,
    'cpython': CPYTHON_VERSION,
    'requirements': requirements,
    'shim': hashlib.sha256(shim_text(bundle).encode()).hexdigest(),
    'source_commit': source_commit,
    'target': list(TARGET),
    'wheels': {wheel.name: _file_digest(wheel) for wheel in wheels},
  }


def built(root: Path) -> Bundle:
  """the bundle at `root`, refusing one never built or left incomplete."""
  bundle = Bundle(root)
  missing = bundle.missing()
  if len(missing) > 0:
    absent = ', '.join(str(part) for part in missing)
    raise FileNotFoundError(f'no bundle at {root} ({absent} absent); build it with {__cli_name__}')
  _ = bundle.identity
  return bundle


def workspace_root() -> Path:
  """the framework checkout the bundle's contents are resolved from.

  The framework is installed from it by path, so the running `bro` package sits
  inside the very checkout whose lock pins the build.
  """
  root = SOURCE_ROOT.parent
  lock = root / 'uv.lock'
  if not lock.is_file():
    raise FileNotFoundError(f'{root} is no framework checkout: {lock} is absent')
  return root


def default_root(workspace: Path) -> Path:
  return workspace / 'var' / 'benchmark' / 'bundle'


def host_mismatch() -> Optional[str]:
  """what disqualifies this machine from building the bundle, if anything."""
  libc = platform.libc_ver()[0]
  host = (sys.platform, platform.machine(), libc if libc != '' else 'unrecognised-libc')
  if host != TARGET:
    return f'the bundle targets {"/".join(TARGET)}; this host is {"/".join(host)}'
  return None


def python_install_command(into: Path) -> list[str]:
  return ['uv', 'python', 'install', '--install-dir', str(into), '--no-bin', CPYTHON_VERSION]


def export_command(workspace: Path) -> list[str]:
  selection = [argument for package in WHEEL_PACKAGES for argument in ('--package', package)]
  return [
    'uv',
    'export',
    '--project',
    str(workspace),
    '--frozen',
    '--no-default-groups',
    '--no-emit-workspace',
    *selection,
    '--no-hashes',
    '--no-annotate',
    '--no-header',
    '--format',
    'requirements.txt',
  ]


def wheel_command(workspace: Path, into: Path, package: str) -> list[str]:
  return [
    'uv',
    'build',
    '--project',
    str(workspace),
    '--package',
    package,
    '--wheel',
    '--out-dir',
    str(into),
  ]


def install_command(bundle: Bundle, requirements: Path, wheels: list[Path]) -> list[str]:
  return [
    'uv',
    'pip',
    'install',
    '--target',
    str(bundle.site_packages),
    '--python',
    str(bundle.interpreter),
    '--no-deps',
    '--link-mode',
    'copy',
    '--requirements',
    str(requirements),
    *(str(wheel) for wheel in wheels),
  ]


def _run(command: list[str]) -> None:
  log.verbose('%s', ' '.join(command))
  spawn.run(command, check=True)


def _capture(command: list[str]) -> str:
  log.verbose('%s', ' '.join(command))
  return spawn.run(command, capture_output=True, check=True, text=True).stdout


def _source_commit(workspace: Path) -> str:
  commit = _capture(['git', '-C', str(workspace), 'rev-parse', 'HEAD']).strip()
  if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
    raise ValueError(f'git reported malformed source commit {commit!r}')
  return commit


def _install_interpreter(bundle: Bundle, staging: Path) -> None:
  _run(python_install_command(staging))
  # uv names the installation after the version it resolved and links a
  # minor-version alias beside it whose target is absolute, so the versioned
  # directory is the only part that relocates
  installed = [
    entry
    for entry in staging.iterdir()
    if entry.is_dir() and not entry.is_symlink() and entry.name.startswith('cpython-')
  ]
  if len(installed) != 1:
    found = ', '.join(sorted(entry.name for entry in installed))
    raise RuntimeError(f'expected one CPython under {staging}, found: {found or "none"}')
  installed[0].rename(bundle.python)


@contextlib.contextmanager
def _staging(root: Path) -> Generator[Path]:
  """scratch space for the build, at a path a rebuild repeats.

  Not a temp directory: uv bakes the path it installed at into the interpreter's
  `sysconfig` data and into the framework wheel's recorded origin, so a random
  one leaves two builds of one commit differing. It also has to share a
  filesystem with the bundle — the interpreter renames out of it.
  """
  directory = root / '.build'
  directory.mkdir()
  try:
    yield directory
  finally:
    shutil.rmtree(directory)


def _wheels(directory: Path) -> list[Path]:
  wheels = sorted(directory.glob('*.whl'))
  if len(wheels) != len(WHEEL_PACKAGES):
    found = ', '.join(wheel.name for wheel in wheels)
    raise RuntimeError(
      f'expected {len(WHEEL_PACKAGES)} framework wheels in {directory}, found: {found or "none"}'
    )
  return wheels


def build(workspace: Path, root: Path) -> Bundle:
  mismatch = host_mismatch()
  if mismatch is not None:
    raise RuntimeError(mismatch)
  bundle = Bundle(root)
  if root.exists():
    shutil.rmtree(root)
  root.mkdir(parents=True)
  source_commit = _source_commit(workspace)
  with _staging(root) as staging:
    log.info('installing CPython %s', CPYTHON_VERSION)
    _install_interpreter(bundle, staging / 'interpreter')
    requirements = staging / 'requirements.txt'
    requirements_text = _capture(export_command(workspace))
    requirements.write_text(requirements_text)
    log.info('building the framework wheels')
    for package in WHEEL_PACKAGES:
      _run(wheel_command(workspace, staging / 'wheel', package))
    wheels = _wheels(staging / 'wheel')
    log.info('installing the framework into %s', bundle.site_packages)
    _run(install_command(bundle, requirements, wheels))
    manifest = _bundle_manifest(bundle, requirements_text, wheels, source_commit)
    bundle.manifest.write_bytes(_manifest_bytes(manifest) + b'\n')
  bundle.shim.write_text(shim_text(bundle))
  bundle.shim.chmod(0o755)
  return built(root)


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='build the relocatable bro bundle')
  parser.add_argument(
    '--output', help='directory to build into (default: <checkout>/var/benchmark/bundle)'
  )
  args = parser.parse(argv)
  workspace = workspace_root()
  output = args['output']
  root = default_root(workspace) if output is None else Path(output).resolve()
  try:
    bundle = build(workspace, root)
  except subprocess.CalledProcessError as error:
    log.error('%s failed: %s', error.cmd[0], error.stderr or error)
    return 1
  print(bundle.root)
  return None


if __name__ == '__main__':
  sys.exit(main(sys.argv))
