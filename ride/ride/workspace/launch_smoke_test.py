"""live check of the container launch path: a cold image tag must build, and the
container prepared from it must reach running state.

Drives the real host docker daemon through the production entry points: runtime
bundle resolution, runtime/project image builds, volume materialization,
`prepare_container`, and `docker start`. The images use repositories owned by the
test and both tags are removed before resolution, forcing the cold-build path.
"""

import contextlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import bro.workspace.project as workspace_project
import ride.workspace.docker as workspace_docker
from ride.repository import Repository
from ride.runtime_bundle import resolve_runtime_bundle
from ride.workspace.docker import Launch
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace


def _docker_available() -> bool:
  try:
    return subprocess.run(['docker', 'info'], capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


pytestmark = [
  pytest.mark.skipif(
    Path('/.dockerenv').is_file(), reason='host-only: drives the host docker daemon'
  ),
  pytest.mark.skipif(not _docker_available(), reason='no reachable docker daemon'),
]

_IMAGE_REPOSITORY = 'bro/launch-smoke-test'
_WORKSPACE_NAME = 'launch-smoke-test'
_READY_MARKER = '.launch-smoke-ready'
# the session command. it marks the shared workspace mount because a container is
# already `Running` while its entrypoint executes — only the command's own signal
# says the entrypoint handed over. then it outlives the observation, bounded so a
# container left behind by a crashed teardown still goes away on its own
_SESSION_COMMAND = ['bash', '-c', f'touch /workspace/{_READY_MARKER}; sleep 300']
_RUNNING_TIMEOUT = 120.0


@dataclass(frozen=True)
class Isolated:
  """the throwaway tree one launch runs against."""

  project: Path


@dataclass(frozen=True)
class Launched:
  """what one cold-tag launch showed."""

  tag: str
  image_before: bool
  image_after: bool
  running: bool
  workspace: Path
  output: str


def _checkout() -> Path:
  """the tree this test ships in — what the image must be built from.

  not `project_root()`: from a linked worktree that resolves to the main
  checkout, and a gate run has to build the sources it is checking.
  """
  toplevel = subprocess.run(
    ['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', '--show-toplevel'],
    capture_output=True,
    text=True,
    check=True,
  )
  return Path(toplevel.stdout.strip())


def _image_present(tag: str) -> bool:
  return subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0


@contextlib.contextmanager
def _cold_image(tag: str) -> Iterator[None]:
  """own the tag for the whole launch: absent going in, gone coming out."""
  subprocess.run(['docker', 'image', 'rm', '-f', tag], capture_output=True)
  try:
    yield
  finally:
    subprocess.run(['docker', 'image', 'rm', '-f', tag], capture_output=True)


def _wait_running(marker: Path, container_id: str, attached: subprocess.Popen[str]) -> bool:
  deadline = time.monotonic() + _RUNNING_TIMEOUT
  while time.monotonic() < deadline:
    if marker.is_file():
      return workspace_docker.container_running(container_id)
    if attached.poll() is not None:
      return False
    time.sleep(0.2)
  return False


def _drain(attached: subprocess.Popen[str]) -> str:
  try:
    return attached.communicate(timeout=30)[0]
  except subprocess.TimeoutExpired:
    attached.kill()
    return attached.communicate()[0]


@contextlib.contextmanager
def _running_container(container_id: str) -> Iterator[subprocess.Popen[str]]:
  """the prepared container's lifetime, attached so that one which dies inside its
  entrypoint leaves its output behind. removing it is what ends the attach, so the
  output is drained after this scope, not inside it."""
  attached = subprocess.Popen(
    ['docker', 'start', '-a', container_id],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  try:
    yield attached
  finally:
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)


def _start_and_observe(container_id: str, workspace: Path) -> tuple[bool, str]:
  with _running_container(container_id) as attached:
    running = _wait_running(workspace / _READY_MARKER, container_id, attached)
  return running, _drain(attached)


@pytest.fixture(scope='module')
def isolated() -> Iterator[Isolated]:
  """a standalone clone of the checkout.

  the launch mounts the clone at /host-repo and roots its workspace dir under it.
  a clone rather than the checkout itself because the entrypoint clones that mount
  with `--shared`, which a linked worktree cannot serve.
  """
  root = Path(tempfile.mkdtemp(prefix='bro-launch-smoke-'))
  checkout = _checkout()
  project = root / 'project'
  subprocess.run(['git', 'clone', '--quiet', str(checkout), str(project)], check=True)
  origin = subprocess.run(
    ['git', '-C', str(checkout), 'remote', 'get-url', 'origin'],
    capture_output=True,
    text=True,
    check=True,
  )
  subprocess.run(
    ['git', '-C', str(project), 'remote', 'set-url', 'origin', origin.stdout.strip()], check=True
  )
  # a clone mirrors the source's local branches only, so seed the ref the entrypoint refreshes
  subprocess.run(
    ['git', '-C', str(project), 'update-ref', 'refs/remotes/origin/master', 'HEAD'], check=True
  )
  yield Isolated(project=project)
  shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope='module')
def launched(isolated: Isolated) -> Iterator[Launched]:
  checkout = _checkout()
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setenv('XDG_DATA_HOME', str(isolated.project.parent / 'state'))
    config = replace(workspace_project.project_config(checkout), image_repository=_IMAGE_REPOSITORY)
    monkeypatch.setattr(Repository, 'project_config', lambda _repository: config)
    monkeypatch.setattr(workspace_docker, '_RUNTIME_IMAGE_REPOSITORY', 'bro/launch-smoke-runtime')
    with resolve_runtime_bundle() as bundle:
      runtime_tag = workspace_docker.runtime_image_tag(bundle.python_version)
      project_tag = workspace_docker.project_image_tag(runtime_tag, checkout)
      assert project_tag is not None
      with contextlib.ExitStack() as stack:
        stack.enter_context(_cold_image(runtime_tag))
        stack.enter_context(_cold_image(project_tag))
        image_before = _image_present(project_tag)
        runtime = workspace_docker.ContainerRuntimeResolver(bundle, isolated.project).resolve()
        launch = Launch(
          name=_WORKSPACE_NAME,
          command=_SESSION_COMMAND,
          env={},
          secrets=(),
          tty=False,
          forward_env=False,
          image=runtime.image,
          runtime_bundle_hash=runtime.bundle_hash,
          repo=isolated.project,
        )
        recorded = Workspace.create(_WORKSPACE_NAME, isolated.project, WorkspaceKind.CONTAINER)
        container_id = workspace_docker.prepare_container(launch)
        running, output = _start_and_observe(container_id, recorded.tree)
        yield Launched(
          tag=project_tag,
          image_before=image_before,
          image_after=_image_present(project_tag),
          running=running,
          workspace=recorded.tree,
          output=output,
        )


def test_cold_project_image_is_built(launched: Launched) -> None:
  assert launched.image_before is False, f'{launched.tag} survived removal, so nothing was built'
  assert launched.image_after is True


def test_the_prepared_container_reaches_running_state(launched: Launched) -> None:
  assert launched.running is True, launched.output


def test_the_workspace_mount_carries_the_clone(launched: Launched) -> None:
  assert (launched.workspace / '.git').is_dir(), launched.output
