"""what a test driving the real docker daemon needs from the host it runs on.

A daemon behind a VM (colima, Docker Desktop) bind-mounts only the host paths its
file sharing covers, and macOS roots its temp directory at `/var/folders`, outside
every default share — a tree rooted there mounts as an empty directory rather than
failing. The checkout is inside a share wherever the product works at all, since
mounting the project is what a managed launch does, so a throwaway tree roots there.

The CLI reads which daemon to talk to out of `$HOME/.docker`, so a scenario that
swaps `HOME` for an isolated one reaches the default endpoint instead of the host's.
`DOCKER_HOST` carries the resolved endpoint across the swap."""

import contextlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRATCH_PREFIX = '.scratch-'


def _daemon_reachable() -> bool:
  try:
    return subprocess.run(['docker', 'info'], capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


HOST_DAEMON_ONLY = [
  pytest.mark.skipif(
    Path('/.dockerenv').is_file(), reason='host-only: drives the host docker daemon'
  ),
  pytest.mark.skipif(not _daemon_reachable(), reason='no reachable docker daemon'),
]


def checkout() -> Path:
  """the tree this test ships in — what an image under test must be built from.

  not `project_root()`: from a linked worktree that resolves to the main checkout,
  and a gate run has to build the sources it is checking."""
  toplevel = subprocess.run(
    ['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', '--show-toplevel'],
    capture_output=True,
    text=True,
    check=True,
  )
  return Path(toplevel.stdout.strip())


@contextlib.contextmanager
def scratch_root(name: str) -> Iterator[Path]:
  """a throwaway tree the host daemon can bind-mount, for one suite's lifetime."""
  root = Path(tempfile.mkdtemp(prefix=f'{_SCRATCH_PREFIX}{name}-', dir=checkout()))
  try:
    yield root
  finally:
    shutil.rmtree(root, ignore_errors=True)


def daemon_endpoint() -> str:
  """the daemon address the host's own CLI resolves, as a `DOCKER_HOST` value."""
  resolved = subprocess.run(
    ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
    capture_output=True,
    text=True,
    check=True,
  )
  return resolved.stdout.strip()
