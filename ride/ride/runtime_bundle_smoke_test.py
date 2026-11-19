"""Live Docker coverage of runtime materialization and materializer lifetime."""

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


def test_git_pins_materialize_on_host_and_as_container_root_with_history(monkeypatch):
  import json

  import ride.runtime_bundle as runtime_bundle
  from ride.runtime_bundle_test_helper import tagged_git_bundle

  with host_docker.scratch_root('git-pins') as root:
    frozen = tagged_git_bundle(root)
    bundle = frozen.bundle
    materialize = runtime_bundle._materialize
    observed: list[Path] = []

    def install(*arguments, **keywords):
      repositories = list((bundle.root / 'git-pins').iterdir())
      assert len(repositories) == 1
      assert repositories[0].stat().st_uid == os.getuid()
      observed.extend(repositories)
      with frozen.source_hidden():
        materialize(*arguments, **keywords)

    monkeypatch.setattr(runtime_bundle, '_materialize', install)
    monkeypatch.setenv('UV_NO_CACHE', '1')
    bundle.materialize_host()
    assert not (bundle.root / 'git-pins').exists()
    image = workspace_docker.runtime_image_tag(bundle.python_version)
    workspace_docker.ensure_runtime_image(image, bundle.python_version, prune=False)
    with contextlib.ExitStack() as cleanup:
      cleanup.callback(
        subprocess.run,
        ['docker', 'volume', 'rm', '-f', bundle.container_volume],
        capture_output=True,
      )
      bundle.materialize_container(image, preflight=lambda: None)
      assert not (bundle.root / 'git-pins').exists()
      assert len(observed) == 2
      script = """\
import importlib.metadata
import json
import os
distribution = importlib.metadata.distribution('demo')
print(json.dumps({'uid': os.getuid(), 'version': distribution.version,
                  'source': json.loads(distribution.read_text('direct_url.json'))}))
"""
      installed = subprocess.run(
        [
          'docker',
          'run',
          '--rm',
          '--entrypoint',
          '/var/ride/runtime/venv/bin/python',
          '-v',
          f'{bundle.container_volume}:/var/ride/runtime:ro',
          image,
          '-c',
          script,
        ],
        check=True,
        capture_output=True,
        text=True,
      )
      result = json.loads(installed.stdout)
      assert result['uid'] == 0
      assert result['uid'] != os.getuid(), 'the host-only test must launch as a non-root user'
      assert result['version'] == frozen.version
      assert result['source']['url'] == frozen.source.as_uri()
      assert result['source']['vcs_info']['commit_id'] == frozen.commit
