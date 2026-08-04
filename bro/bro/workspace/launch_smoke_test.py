"""live check of the container launch path: a cold image tag must build, and the
container prepared from it must reach running state.

Drives the real host docker daemon through the production entry points —
`image_tag` → `_ensure_image` → `prepare_container` → `docker start`. Two
properties the run depends on:

- the image is tagged into a repository of this test's own, because
  `_ensure_image` untags every superseded image of the repository it builds
  into, and a real session's images must stay out of that reach;
- the tag is removed before the launch, so the build branch is the only one
  `_ensure_image` can take.
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

import bro.workspace.docker as workspace_docker
import bro.workspace.project as workspace_project
from bro.workspace.docker import Launch
from bro.workspace.paths import containers_dir


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
  """the throwaway tree and home one launch runs against."""

  project: Path
  home: Path


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
  """a standalone clone of the checkout plus a synthetic home.

  the launch mounts the clone at /host-repo and roots its workspace dir under it.
  a clone rather than the checkout itself because the entrypoint clones that mount
  with `--shared`, which a linked worktree cannot serve. the home carries the
  `.gitconfig` the launch bind-mounts, keeping the real one off the container.
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
  home = root / 'home'
  home.mkdir()
  (home / '.gitconfig').write_text(
    '[user]\n\tname = launch smoke\n\temail = smoke@invalid\n[init]\n\tdefaultBranch = master\n'
  )
  yield Isolated(project=project, home=home)
  shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope='module')
def launched(isolated: Isolated) -> Iterator[Launched]:
  checkout = _checkout()
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setattr(workspace_docker, 'project_root', lambda: checkout)
    monkeypatch.setattr(workspace_project, 'project_root', lambda: checkout)
    monkeypatch.setattr(workspace_docker.Path, 'home', lambda: isolated.home)
    config = replace(workspace_project.project_config(), image_repository=_IMAGE_REPOSITORY)
    monkeypatch.setattr(workspace_docker, 'project_config', lambda: config)
    tag = workspace_docker.image_tag()
    with _cold_image(tag):
      image_before = _image_present(tag)
      launch = Launch(
        name=_WORKSPACE_NAME,
        command=_SESSION_COMMAND,
        # skips the entrypoint's venv-dependent half, which costs a full `uv sync`
        # whenever the clone's committed manifests differ from the ones the image
        # baked — an uncommitted manifest edit would otherwise stall the gate
        env={'CW_SKIP_VENV': '1'},
        secrets=(),
        docker_sock=False,
        tty=False,
        forward_env=False,
      )
      container_id = workspace_docker.prepare_container(launch, isolated.project)
      workspace = containers_dir(isolated.project) / _WORKSPACE_NAME
      running, output = _start_and_observe(container_id, workspace)
      yield Launched(
        tag=tag,
        image_before=image_before,
        image_after=_image_present(tag),
        running=running,
        workspace=workspace,
        output=output,
      )


def test_ensure_image_builds_the_cold_tag(launched: Launched) -> None:
  assert launched.image_before is False, f'{launched.tag} survived removal, so nothing was built'
  assert launched.image_after is True


def test_the_prepared_container_reaches_running_state(launched: Launched) -> None:
  assert launched.running is True, launched.output


def test_the_workspace_mount_carries_the_clone(launched: Launched) -> None:
  assert (launched.workspace / '.git').is_dir(), launched.output
