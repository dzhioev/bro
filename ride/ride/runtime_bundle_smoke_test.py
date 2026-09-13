"""live check of the materializer container's process-tethered lifetime.

Drives the real host docker daemon: the process holding a materializer container
is SIGKILLed, so no Python teardown runs, and the daemon on its own must observe
the container exit and remove it.
"""

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import ride.workspace.docker as workspace_docker
import ride.workspace.host_docker_test_helper as host_docker

pytestmark = host_docker.HOST_DAEMON_ONLY

_IMAGE = 'busybox'
_REMOVAL_TIMEOUT = 60.0

_HOLDER = """\
import signal
import sys
from pathlib import Path

from ride.runtime_bundle import RuntimeBundle, _materializer_container

bundle = RuntimeBundle(Path(sys.argv[1]), '3.12')
with _materializer_container(bundle, sys.argv[2]) as container_id:
  print(container_id, flush=True)
  signal.pause()
"""


@contextlib.contextmanager
def _held_materializer(bundle_root: Path, image: str) -> Iterator[tuple[subprocess.Popen, str]]:
  """a child process holding a live materializer container, and that container's id.

  The holder gets a stdin pipe this process keeps open through the whole scope, so a
  materializer that inherits its launcher's stdin instead of owning a private pipe
  keeps its container alive past the holder's death and fails the removal wait."""
  holder = subprocess.Popen(
    [sys.executable, '-c', _HOLDER, str(bundle_root), image],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
  )
  container_id = ''
  try:
    assert holder.stdout is not None
    container_id = holder.stdout.readline().strip()
    if container_id == '':
      raise AssertionError(f'holder printed no container id: {holder.communicate()[1]}')
    yield holder, container_id
  finally:
    if holder.poll() is None:
      holder.kill()
    holder.wait()
    if container_id != '':
      subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)
    subprocess.run(
      ['docker', 'volume', 'rm', f'ride-runtime-{bundle_root.name}'], capture_output=True
    )


def _assert_removed(container_id: str) -> None:
  deadline = time.monotonic() + _REMOVAL_TIMEOUT
  while True:
    inspected = subprocess.run(
      ['docker', 'container', 'inspect', container_id], capture_output=True, text=True
    )
    if inspected.returncode != 0:
      assert 'no such' in inspected.stderr.lower(), inspected.stderr
      return
    assert time.monotonic() < deadline, (
      f'container {container_id} still exists after its holder was killed'
    )
    time.sleep(0.2)


def test_killing_the_holder_ends_and_removes_the_container():
  with host_docker.scratch_root('materializer-kill') as root:
    bundle_root = root / os.urandom(32).hex()
    bundle_root.mkdir()
    with _held_materializer(bundle_root, _IMAGE) as (holder, container_id):
      assert workspace_docker.container_running(container_id)
      os.kill(holder.pid, signal.SIGKILL)
      holder.wait()
      # blocks until the container exits; already-removed is the same outcome arriving early
      subprocess.run(
        ['docker', 'wait', container_id], capture_output=True, timeout=_REMOVAL_TIMEOUT
      )
      _assert_removed(container_id)
