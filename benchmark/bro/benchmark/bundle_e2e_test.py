"""live check of a built bundle riding a bro in an image with no Python of its own.

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
import json
import subprocess
from collections.abc import Generator

import pytest

from bro.benchmark.bundle import build, claude_code_cache, host_mismatch, workspace_root
from bro.benchmark.harbor_agent import ride_command

IMAGE = 'ubuntu:24.04'
INSTALL_DIRECTORY = '/installed-agent'


def _docker_available() -> bool:
  try:
    return subprocess.run(['docker', 'info'], capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


_HOST_MISMATCH = host_mismatch()

pytestmark = [
  pytest.mark.skipif(not _docker_available(), reason='no reachable docker daemon'),
  pytest.mark.skipif(_HOST_MISMATCH is not None, reason=str(_HOST_MISMATCH)),
]


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


def test_the_bundle_rides_a_bro_where_no_python_is_installed(tmp_path):
  workspace = workspace_root()
  bundle = build(workspace, tmp_path / 'bundle', claude_code_cache(workspace))
  claude_code = json.loads(bundle.manifest.read_text())['claude_code']

  with _container() as container:
    absent = _in(container, 'sh', '-c', 'command -v python3 python || true')
    assert absent == ''
    _in(container, 'mkdir', '--parents', f'{INSTALL_DIRECTORY}/credentials', '/task', '/logs/agent')
    _docker('cp', str(bundle.root), f'{container}:{INSTALL_DIRECTORY}/bro')
    installed = f'{INSTALL_DIRECTORY}/bro'

    listed = _in(container, f'{installed}/venv/bin/bro', 'list')
    card = _in(container, f'{installed}/bin/bro', 'show', 'terminal')
    claude_version = _in(container, f'{installed}/claude/claude', '--version')
    # the trial's own launch, on the provider that answers without a key
    ride = ride_command(bro='terminal', instruction='say hello', harness='bro', llm='echo:')
    _in(
      container,
      'env',
      '-i',
      'HOME=/root',
      'PATH=/usr/local/bin:/usr/bin:/bin',
      'XDG_DATA_HOME=/logs/agent',
      f'BRO_STORE={INSTALL_DIRECTORY}/credentials',
      'sh',
      '-c',
      f'cd /task && {ride}',
    )
    trails = _in(container, 'sh', '-c', 'ls /logs/agent/ride/trails/trails | wc -l')
    workspaces = _in(container, 'sh', '-c', 'ls /logs/agent/ride/workspaces | wc -l')

  assert 'terminal: ' in listed
  assert card.startswith('# terminal')
  assert claude_version.startswith(claude_code['version'])
  assert trails.strip() == '1'
  assert workspaces.strip() == '1'
