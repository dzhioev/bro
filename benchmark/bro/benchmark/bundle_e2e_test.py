"""live check of a built bundle running a bro in an image with no Python of its own.

`ubuntu:24.04` is the base most Terminal-Bench task images derive from, and
`--network none` leaves the container nothing to fetch at run time. The bundle
crosses in over `docker cp` — the same tar stream harbor's `upload_dir` uses —
rather than a bind mount, which needs the bundle on the docker *host*'s
filesystem and so would not run from inside a container.

It drives a real daemon and builds a real bundle, so it stays out of the gate's
roster:

  uv run --directory benchmark pytest bro/benchmark/bundle_e2e_test.py
"""

import contextlib
import subprocess
from collections.abc import Generator

import pytest

from bro.benchmark.bundle import build, workspace_root

IMAGE = 'ubuntu:24.04'
INSTALL_DIRECTORY = '/installed-agent'


def _docker_available() -> bool:
  try:
    return subprocess.run(['docker', 'info'], capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason='no reachable docker daemon')


def _docker(*arguments: str) -> str:
  return subprocess.run(
    ['docker', *arguments], capture_output=True, text=True, check=True
  ).stdout.strip()


@contextlib.contextmanager
def _container() -> Generator[str]:
  container = _docker('create', '--network', 'none', IMAGE, 'sleep', 'infinity')
  try:
    _docker('start', container)
    yield container
  finally:
    _docker('rm', '--force', container)


def _in(container: str, *command: str) -> str:
  return _docker('exec', container, *command)


def test_the_bundle_runs_a_bro_where_no_python_is_installed(tmp_path):
  bundle = build(workspace_root(), tmp_path / 'bundle')

  with _container() as container:
    absent = _in(container, 'sh', '-c', 'command -v python3 python || true')
    assert absent == ''
    _in(container, 'mkdir', '--parents', INSTALL_DIRECTORY)
    _docker('cp', str(bundle.root), f'{container}:{INSTALL_DIRECTORY}/bro')
    shim = f'{INSTALL_DIRECTORY}/bro/bro'

    listed = _in(container, shim, 'list')
    card = _in(container, shim, 'show', 'terminal')

  assert 'terminal: ' in listed
  assert card.startswith('# terminal')
