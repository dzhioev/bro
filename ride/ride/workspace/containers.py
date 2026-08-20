import os
import subprocess
import sys

from bro.base import log
from bro.workspace.paths import project_root
from ride.workspace.docker import (
  DETACH_FLAG,
  container_running,
  find_container_id,
  suspend_until_continued,
)
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace


def exec_in_workspace(name: str, command: list[str]) -> int:
  """exec a command in the running container backing the named workspace."""
  project = project_root()
  try:
    workspace = Workspace.open(name, project)
  except ValueError as e:
    log.error('%s', e)
    return 1
  if workspace.kind is not WorkspaceKind.CONTAINER:
    log.error(
      'workspace %r is a %s workspace; there is no container to exec into', name, workspace.kind
    )
    return 1
  container_id = find_container_id(workspace.tree)
  if container_id is None:
    log.error('no running container for workspace %r', name)
    return 1
  docker_command = ['bash'] if len(command) == 0 else command
  # run as ride, not the image's default root: docker exec ignores the entrypoint's
  # gosu drop, so without -u every exec'd command runs as root and writes
  # root-owned files into the bind-mounted /workspace that the host user can't
  # later remove. the entrypoint remaps ride to the host uid, so -u ride matches the
  # session user and keeps workspace files host-owned.
  return subprocess.run(
    ['docker', 'exec', '-it', '-u', 'ride', container_id, *docker_command]
  ).returncode


def broker_enabled() -> bool:
  """whether this launch runs under the broker (a channel for every session, host
  and container alike).

  `BROKER_DISABLED` is the presence-checked kill-switch (parallel to `TRAILS_DISABLED`):
  the broker sits on the critical launch path of every session, so a broker defect
  needs an escape valve that works without touching code. It is checked before
  any broker import, and an unimportable broker package (an environment provisioned
  before broker existed) degrades to the broker-less path with a warning — the gate
  itself can never break a launch.
  """
  if os.environ.get('BROKER_DISABLED') is not None:
    return False
  try:
    import bro.broker  # noqa: F401
  except ImportError:
    log.warning('broker package not importable; launching without a broker channel')
    return False
  return True


def container_broker_enabled() -> bool:
  """`broker_enabled`, plus the container flavor's docker-daemon constraint.

  the container's channel is the host socket bind-mounted at `/run/broker.sock`,
  which requires a docker daemon that shares the host filesystem. on macOS the
  daemon runs in a VM (Docker Desktop / colima) whose file sharing cannot project
  a host unix socket: the mount is unappliable — which breaks container creation
  outright, because the scoped store's `docker cp` stats its destination by
  mounting the whole container filesystem — and even a mounted socket file could
  not carry connections across the VM boundary. container sessions there run
  broker-less; the host flavor keeps its channel (its socket is reached
  in-process, no daemon in between).
  """
  if sys.platform == 'darwin':
    log.info('no broker channel: the macOS docker daemon cannot bind-mount host unix sockets')
    return False
  return broker_enabled()


def attach_interactive(container_id: str) -> int:
  """run the interactive docker client, turning a Ctrl+Z detach into a job-control
  suspend: a zero client exit with the container still running is the detach key
  firing — freeze the session until the shell resumes it, then re-attach."""
  code = subprocess.run(['docker', 'start', '-a', '-i', DETACH_FLAG, container_id]).returncode
  while code == 0 and container_running(container_id):
    suspend_until_continued(container_id)
    code = subprocess.run(['docker', 'attach', DETACH_FLAG, container_id]).returncode
  return code
