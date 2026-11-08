"""The host prerequisites a managed launch needs, probed and fixed per platform."""

import os
import platform
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.workspace.paths import ISOLATION_ENV
from ride.workspace.build_context import UV_VERSION_FILE


class Platform(Enum):
  MACOS = 'macOS'
  UBUNTU = 'Ubuntu'


class UnsupportedHost(Exception):
  pass


@dataclass(frozen=True)
class Fix:
  """a shell line that satisfies a prerequisite; `automatic` ones need no root, and
  `--install` runs them."""

  command: str
  automatic: bool


@dataclass(frozen=True)
class Probe:
  """what one probe found; `problem` is None when the prerequisite holds, and `fix`
  replaces the prerequisite's own when this finding needs another."""

  found: str
  problem: Optional[str] = None
  fix: Optional[Fix] = None


@dataclass(frozen=True)
class Prerequisite:
  name: str
  purpose: str
  probe: Callable[[], Probe]
  fixes: Mapping[Platform, Fix]
  # earlier prerequisites whose fixes come first
  requires: tuple[str, ...] = ()


def _first_line(text: str) -> str:
  return text.strip().partition('\n')[0]


def _reports(*argv: str) -> Probe:
  """holds when `argv` exits 0, having found its first output line."""
  try:
    result = subprocess.run(argv, capture_output=True, text=True)
  except FileNotFoundError:
    return Probe(found='', problem=f'{argv[0]} is not on PATH')
  if result.returncode != 0:
    detail = _first_line(result.stderr) or f'exit status {result.returncode}'
    return Probe(found='', problem=f'`{shlex.join(argv)}` failed: {detail}')
  return Probe(found=_first_line(result.stdout))


def _uv_install_directory() -> Path:
  """where uv's fix installs it: a directory the user named for uv's installer or
  for their own binaries, else the installer's default."""
  for name in ('UV_INSTALL_DIR', 'XDG_BIN_HOME'):
    value = os.environ.get(name, '')
    if value != '':
      return Path(value)
  return Path.home() / '.local' / 'bin'


def _uv_installer(directory: Path) -> Fix:
  pinned = UV_VERSION_FILE.read_text().strip()
  return Fix(
    f'curl -LsSf https://astral.sh/uv/{pinned}/install.sh '
    f'| env UV_INSTALL_DIR={shlex.quote(str(directory))} sh',
    automatic=True,
  )


def _uv_version(executable: str) -> Optional[str]:
  """the version `executable --version` reports, or None when it fails."""
  result = subprocess.run([executable, '--version'], capture_output=True, text=True)
  if result.returncode != 0:
    return None
  words = result.stdout.split()
  if len(words) < 2 or words[0] != 'uv':
    raise ValueError(f'unrecognized `{executable} --version` output: {result.stdout!r}')
  return words[1]


def _uv(directory: Path) -> Probe:
  """holds when PATH selects the pinned uv; one the fix installed into `directory` but
  PATH does not reach first needs PATH changed instead of another install."""
  pinned = UV_VERSION_FILE.read_text().strip()
  selected = shutil.which('uv')
  version = None if selected is None else _uv_version(selected)
  if version == pinned:
    return Probe(f'uv {version} at {selected}')
  if selected is None:
    problem = 'uv is not on PATH'
  elif version is None:
    problem = f'`{selected} --version` failed'
  else:
    problem = f'uv {version} at {selected} is installed, {pinned} is pinned'
  installed = directory / 'uv'
  if not (installed.is_file() and _uv_version(str(installed)) == pinned):
    return Probe('', problem=problem)
  return Probe(
    '',
    problem=f'{problem}, and PATH does not reach the pinned uv at {installed} first',
    fix=Fix(
      f'export PATH={shlex.quote(str(directory))}:"$PATH"  # and in your shell profile',
      automatic=False,
    ),
  )


def prerequisites() -> tuple[Prerequisite, ...]:
  uv_directory = _uv_install_directory()
  return (
    Prerequisite(
      name='git',
      purpose='clones and fetches attached repositories',
      probe=lambda: _reports('git', '--version'),
      fixes={
        Platform.MACOS: Fix('xcode-select --install', automatic=False),
        Platform.UBUNTU: Fix('sudo apt-get install -y git', automatic=False),
      },
    ),
    Prerequisite(
      name='uv',
      purpose='syncs project environments at the version the session images run',
      probe=lambda: _uv(uv_directory),
      fixes=dict.fromkeys(Platform, _uv_installer(uv_directory)),
    ),
    Prerequisite(
      name='docker',
      purpose='runs boxed sessions',
      probe=lambda: _reports('docker', '--version'),
      fixes={
        Platform.MACOS: Fix('brew install colima docker && brew link docker', automatic=True),
        Platform.UBUNTU: Fix('curl -fsSL https://get.docker.com | sudo sh', automatic=False),
      },
    ),
    Prerequisite(
      name='docker daemon',
      purpose='runs boxed sessions',
      probe=lambda: _reports('docker', 'info', '--format', '{{.ServerVersion}}'),
      fixes={
        Platform.MACOS: Fix('brew install colima && colima start', automatic=True),
        Platform.UBUNTU: Fix(
          'sudo systemctl enable --now docker && sudo usermod -aG docker "$USER"'
          '  # the group applies from the next login',
          automatic=False,
        ),
      },
      requires=('docker',),
    ),
    Prerequisite(
      name='docker buildx',
      purpose='builds the session images, whose Dockerfiles need BuildKit',
      probe=lambda: _reports('docker', 'buildx', 'version'),
      fixes={
        # Homebrew links the plugin outside the directories the docker CLI searches
        Platform.MACOS: Fix(
          'brew install docker-buildx && mkdir -p ~/.docker/cli-plugins && ln -sfn '
          '"$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx',
          automatic=True,
        ),
        Platform.UBUNTU: Fix('sudo apt-get install -y docker-buildx-plugin', automatic=False),
      },
      requires=('docker',),
    ),
  )


def host_platform() -> Platform:
  if sys.platform == 'darwin':
    return Platform.MACOS
  if sys.platform == 'linux':
    try:
      release = platform.freedesktop_os_release()
    except OSError:
      release = {}
    if release.get('ID') == 'ubuntu':
      return Platform.UBUNTU
  raise UnsupportedHost(f'unsupported host platform {sys.platform}: only macOS and Ubuntu are')


def _install(prerequisites: Sequence[Prerequisite], host: Platform) -> bool:
  """run the automatic fix of every prerequisite that fails, in order; false once one
  exits nonzero, since a later fix may build on it."""
  unmet: set[str] = set()
  for prerequisite in prerequisites:
    probe = prerequisite.probe()
    if probe.problem is None:
      continue
    fix = probe.fix if probe.fix is not None else prerequisite.fixes[host]
    if not fix.automatic or not unmet.isdisjoint(prerequisite.requires):
      unmet.add(prerequisite.name)
      continue
    log.info('installing %s: %s', prerequisite.name, fix.command)
    status = subprocess.run(
      ['bash', '-o', 'pipefail', '-c', fix.command], stdout=sys.stderr
    ).returncode
    if status != 0:
      log.error('installing %s exited with status %d', prerequisite.name, status)
      return False
  return True


def check_host(prerequisites: Sequence[Prerequisite], host: Platform, *, install: bool) -> int:
  """report every prerequisite with its fix, after running the automatic fixes when
  `install` asks; 0 when all hold."""
  if install and not _install(prerequisites, host):
    return 1
  # each failing prerequisite, and whether `--install` would run its fix
  failing: dict[str, bool] = {}
  for prerequisite in prerequisites:
    probe = prerequisite.probe()
    if probe.problem is None:
      print(f'{prerequisite.name}: ok ({probe.found})')
      continue
    fix = probe.fix if probe.fix is not None else prerequisite.fixes[host]
    unmet = [name for name in prerequisite.requires if name in failing]
    failing[prerequisite.name] = len(unmet) == 0 and fix.automatic
    if len(unmet) > 0:
      print(f'{prerequisite.name}: needs {", ".join(unmet)}')
      continue
    print(f'{prerequisite.name} ({prerequisite.purpose}): {probe.problem}')
    print(f'  fix: {fix.command}')
  automatic = [name for name, fixable in failing.items() if fixable]
  if len(automatic) > 0 and not install:
    print(f'`ride check-host --install` runs the fixes for {", ".join(automatic)}')
  return 1 if len(failing) > 0 else 0


def check_this_host(*, install: bool) -> int:
  if ISOLATION_ENV in os.environ:
    raise UnsupportedHost('check-host checks the launching host; run it outside a managed session')
  return check_host(prerequisites(), host_platform(), install=install)
