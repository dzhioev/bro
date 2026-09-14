#!/usr/bin/env python
"""build the relocatable runtime a machine with no Python runs `ride` from.

The bundle is self-contained: a pinned standalone CPython carrying the framework
distributions a ride runs from, resolved against the workspace lock, laid out as
the materialized `venv/` + `bin/` runtime `ride --runtime-bundle` takes, plus the
standalone Claude Code binary at the version ride pins. Copying the directory
anywhere on a linux/x86_64 glibc machine is the whole installation — nothing
outside it is read.

What the bundle contains is decided by the pinned interpreter version here, the
workspace `uv.lock`, and the Claude Code release manifest, so two builds of one
commit carry the same code.
"""

import contextlib
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import log, spawn
from bro.base.args import Parser
from bro.base.source_root import SOURCE_ROOT
from ride.runtime_bundle import link_session_commands, validate_materialized_runtime
from ride.workspace.build_context import claude_code_version

CPYTHON_VERSION = '3.12.14'
# core, the engine that runs a bro, the distribution every persona but the
# minimal `bro` one ships from, and the launcher a trial rides under
WHEEL_PACKAGES = ('bro', 'bro-native', 'bro-dev', 'bro-ride')
TARGET = ('linux', 'x86_64', 'glibc')
# `TARGET`, as the Claude Code release manifest names it
CLAUDE_CODE_PLATFORM = 'linux-x64'
CLAUDE_CODE_RELEASES = 'https://downloads.claude.ai/claude-code-releases'
MANIFEST_FORMAT = 3

# a console script that finds its interpreter beside itself wherever the bundle
# is copied: sh reads the second line as an `exec`, python as a string
_SCRIPT_PRELUDE = """\
#!/bin/sh
'''exec' "$(dirname -- "$(readlink -f -- "$0")")/python3" -s "$0" "$@"
' '''
"""
# the closing line of the prelude uv itself writes when the interpreter path is
# too long for a shebang
_LONG_SHEBANG_CLOSE = b"' '''"


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
  return json.dumps(manifest, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()


def _digest_valid(value: object) -> bool:
  return (
    isinstance(value, str)
    and len(value) == 64
    and all(character in '0123456789abcdef' for character in value)
  )


def _claude_code_valid(value: object) -> bool:
  return (
    isinstance(value, dict)
    and set(value) == {'version', 'sha256'}
    and isinstance(value['version'], str)
    and value['version'] != ''
    and _digest_valid(value['sha256'])
  )


def _load_manifest(path: Path) -> dict[str, object]:
  try:
    manifest = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError) as error:
    raise ValueError(f'invalid bundle manifest at {path}: {error}') from error
  if not isinstance(manifest, dict) or set(manifest) != {
    'format',
    'claude_code',
    'cpython',
    'requirements',
    'source_commit',
    'target',
    'wheels',
  }:
    raise ValueError(f'invalid bundle manifest at {path}: unexpected fields')
  target = manifest['target']
  wheels = manifest['wheels']
  if (
    manifest['format'] != MANIFEST_FORMAT
    or not _claude_code_valid(manifest['claude_code'])
    or not isinstance(manifest['cpython'], str)
    or manifest['cpython'] == ''
    or not isinstance(manifest['requirements'], str)
    or manifest['requirements'] == ''
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
  def venv(self) -> Path:
    """the standalone CPython, the framework installed into its own site-packages."""
    return self.root / 'venv'

  @property
  def interpreter(self) -> Path:
    return self.venv / 'bin' / 'python3'

  @property
  def shims(self) -> Path:
    """the shim farm over the session commands, what leads a session's PATH."""
    return self.root / 'bin'

  def script(self, name: str) -> Path:
    """one console script the framework installed, `ride` among them."""
    return self.venv / 'bin' / name

  @property
  def claude_dir(self) -> Path:
    """the directory holding the `claude` binary, for a PATH entry of its own."""
    return self.root / 'claude'

  @property
  def claude(self) -> Path:
    return self.claude_dir / 'claude'

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
    parts = (self.interpreter, self.script('ride'), self.shims, self.claude, self.manifest)
    return tuple(part for part in parts if not part.exists())


def _file_digest(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as file:
    while chunk := file.read(1024 * 1024):
      digest.update(chunk)
  return digest.hexdigest()


def _bundle_manifest(
  requirements: str, wheels: list[Path], source_commit: str, claude_code: dict[str, str]
) -> dict[str, object]:
  return {
    'format': MANIFEST_FORMAT,
    'claude_code': claude_code,
    'cpython': CPYTHON_VERSION,
    'requirements': requirements,
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
    raise FileNotFoundError(
      f'no bundle at {root} ({absent} absent); build it with benchmark bundle'
    )
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


def claude_code_cache(workspace: Path) -> Path:
  """where downloaded Claude Code binaries are kept between builds."""
  return workspace / 'var' / 'benchmark' / 'claude-code'


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
  """install into the bundled interpreter itself, so its console scripts land in
  `venv/bin` beside it and its site-packages needs no path of its own."""
  return [
    'uv',
    'pip',
    'install',
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
  installed[0].rename(bundle.venv)
  unversioned = bundle.venv / 'bin' / 'python'
  if not unversioned.exists():
    unversioned.symlink_to('python3')
  # the marker guards uv's own managed installations against installs; this
  # copy is the bundle's to install into
  for marker in bundle.venv.glob('lib/python*/EXTERNALLY-MANAGED'):
    marker.unlink()


def relocate_script(path: Path, venv: Path) -> bool:
  """rewrite a console script the installer pointed at `venv`'s interpreter by
  absolute path to find that interpreter beside itself; answers whether the
  script was one."""
  text = path.read_bytes()
  if not text.startswith(b'#!'):
    return False
  first, _, rest = text.partition(b'\n')
  interpreter_prefix = str(venv / 'bin' / 'python').encode()
  if first == b'#!/bin/sh':
    exec_line, _, rest = rest.partition(b'\n')
    close_line, _, body = rest.partition(b'\n')
    if interpreter_prefix not in exec_line or close_line.rstrip() != _LONG_SHEBANG_CLOSE:
      return False
  elif first.startswith(b'#!' + interpreter_prefix):
    body = rest
  else:
    return False
  path.write_bytes(_SCRIPT_PRELUDE.encode() + body)
  return True


def _relocate_scripts(bundle: Bundle) -> None:
  relocated = [
    path.name
    for path in sorted((bundle.venv / 'bin').iterdir())
    if path.is_file() and not path.is_symlink() and relocate_script(path, bundle.venv)
  ]
  if len(relocated) == 0:
    raise RuntimeError(f'no console script under {bundle.venv / "bin"} names the interpreter')
  log.verbose('relocated %d console scripts', len(relocated))


def _fetch_json(url: str) -> dict[str, object]:
  with urllib.request.urlopen(url) as response:
    manifest = json.load(response)
  if not isinstance(manifest, dict):
    raise ValueError(f'{url} is not a JSON object')
  return manifest


def claude_code_checksum(release_manifest: dict[str, object]) -> str:
  """the digest the Claude Code release manifest states for `CLAUDE_CODE_PLATFORM`."""
  platforms = release_manifest.get('platforms')
  if not isinstance(platforms, dict) or CLAUDE_CODE_PLATFORM not in platforms:
    raise ValueError(f'the Claude Code release manifest has no {CLAUDE_CODE_PLATFORM} platform')
  entry = platforms[CLAUDE_CODE_PLATFORM]
  checksum = entry.get('checksum') if isinstance(entry, dict) else None
  if not _digest_valid(checksum):
    raise ValueError(f'the Claude Code release manifest has no {CLAUDE_CODE_PLATFORM} checksum')
  return str(checksum)


def _download(url: str, into: Path) -> None:
  into.parent.mkdir(parents=True, exist_ok=True)
  with urllib.request.urlopen(url) as response, into.open('wb') as file:
    shutil.copyfileobj(response, file)


def cached_claude_code(version: str, cache: Path) -> Path:
  """the Claude Code binary of `version`, downloaded into `cache` unless a copy
  matching the release manifest's checksum is already there."""
  release = f'{CLAUDE_CODE_RELEASES}/{version}'
  checksum = claude_code_checksum(_fetch_json(f'{release}/manifest.json'))
  binary = cache / version / 'claude'
  if binary.is_file() and _file_digest(binary) == checksum:
    return binary
  log.info('downloading Claude Code %s', version)
  staged = binary.with_suffix('.download')
  _download(f'{release}/{CLAUDE_CODE_PLATFORM}/claude', staged)
  digest = _file_digest(staged)
  if digest != checksum:
    staged.unlink()
    raise ValueError(
      f'Claude Code {version} for {CLAUDE_CODE_PLATFORM} downloaded with digest {digest}, '
      f'the release manifest states {checksum}'
    )
  staged.replace(binary)
  return binary


def _install_claude_code(bundle: Bundle, cache: Path) -> dict[str, str]:
  version = claude_code_version()
  binary = cached_claude_code(version, cache)
  bundle.claude_dir.mkdir()
  shutil.copyfile(binary, bundle.claude)
  bundle.claude.chmod(0o755)
  return {'version': version, 'sha256': _file_digest(bundle.claude)}


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


def build(workspace: Path, root: Path, cache: Path) -> Bundle:
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
    log.info('installing the framework into %s', bundle.venv)
    _run(install_command(bundle, requirements, wheels))
    _relocate_scripts(bundle)
    link_session_commands(bundle.root)
    claude_code = _install_claude_code(bundle, cache)
    validate_materialized_runtime(bundle.root)
    manifest = _bundle_manifest(requirements_text, wheels, source_commit, claude_code)
    bundle.manifest.write_bytes(_manifest_bytes(manifest) + b'\n')
  return built(root)


def command(output: Optional[str]) -> Optional[int]:
  workspace = workspace_root()
  root = default_root(workspace) if output is None else Path(output).resolve()
  try:
    bundle = build(workspace, root, claude_code_cache(workspace))
  except subprocess.CalledProcessError as error:
    log.error('%s failed: %s', error.cmd[0], error.stderr or error)
    return 1
  print(bundle.root)
  return None


def _configure_arguments(parser: Parser) -> None:
  parser.add_argument(
    '--output', help='directory to build into (default: <checkout>/var/benchmark/bundle)'
  )


def configure_parser(parser: Parser) -> None:
  _configure_arguments(parser)
  parser.set_handler(command)


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='build the relocatable bro bundle')
  _configure_arguments(parser)
  return command(**parser.parse(argv))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
