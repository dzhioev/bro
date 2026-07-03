"""cw's adapters for broker's `Spawner` port, plus the root-peer launch both use.

`DockerSpawner` pairs with `UnixServerTransport` across the container boundary: the
broker provisions a per-peer host socket, and this spawner bind-mounts it into the
child at the fixed `/run/broker.sock` and points `BROKER_CHANNEL` at it, so the
child's `broker` client connects back over the channel the host supervises.

Two docker launch modes, keyed by `launch.attached` — stdio wiring only; supervision
by the broker's Runtime is uniform:

- Non-TTY (`attached == False`, a spawned child): `docker create` without `-it` (a
  headless child gets no pty) → `docker start -a` as an asyncio subprocess with
  stdout and stderr merged into one pipe and drained by an async task into a bounded
  ring buffer for `failed{output_tail}` — the streams are diagnostics for a child
  that dies without reporting; its result is channel-native (`completed{result}`).
  Attaching (rather than detaching + `docker wait` + `docker logs`) is deliberate:
  detached `--rm` removal races the log read. A derived `broker-<channel>` workspace
  is removed (both host dirs) once the child ends; a named one stays caller-owned.
- Interactive (`attached == True`, e.g. a cw session root): `docker create -it` →
  `docker start -a -i` as an asyncio subprocess with inherited stdio (the host TTY),
  plus a SIGINT forwarder — an interrupt of the launcher must reach the attached
  docker client, not unwind the broker loop out from under the running session.
  `output_tail()` is empty (the streams belong to the TTY).

Either way the exit code is the attached process's returncode and `kill()`
force-removes the container.

`ProcessSpawner` is the host-mode sibling: the peer is a plain subprocess with
inherited stdio (the interactive root of a host session), and the provisioned socket
path is directly reachable from it, so `BROKER_CHANNEL` points straight at the host
endpoint — no bind-mount hop.

`run_root_via_broker` is the launch shape both modes share: one broker over the
`var/cw/broker` control dir, the built-in ping handler, supervise until the root
exits.
"""

import asyncio
import signal
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import credentials, log
from broker.brotocol import Tag
from broker.dispatcher import Broker, ping_handler
from broker.spawn import ChildHandle, LaunchSpec, Spawner
from broker.transport import Provisioned
from broker.transports.unix import UnixServerTransport
from cw.docker import _create_container, _docker_create_argv, _ensure_image, _image_tag
from cw.paths import _broker_dir, _containers_dir, _project_root
from cw.secrets import _ppp_tarball
from cw.workspace import ContainerWorkspace

DEFAULT_RING_BYTES = 1 << 16  # 64 KiB — a full traceback + context, bounded

_IN_CONTAINER_SOCK = '/run/broker.sock'  # short fixed path inside the container (sun_path budget)
_BROKER_ADDRESS = f'unix:{_IN_CONTAINER_SOCK}'
_DRAIN_CHUNK = 65536


@dataclass(frozen=True)
class DockerLaunchSpec(LaunchSpec):
  """the concrete launch description `DockerSpawner` reads.

  `env` is an explicit snapshot the constructor assembles, never a live `os.environ`
  read, so a spawn is a pure function of its `LaunchSpec` — reproducible and
  independent of whatever ambient environment the launcher holds when the loop reaches
  this peer. `bro` is the role stamped into `CW_BRO` (theming + secret scoping), `None`
  for a substrate-native peer with no bro.

  `name` is the workspace backing the container (`var/cw/containers/<name>`); `None`
  derives a throwaway `broker-<channel>` one, removed when the child ends. `secrets`
  hydrate strictly,
  `optional_secrets` best-effort (`credentials.build_scoped_store`). `docker_sock` and
  `forward_bro` mirror the `_docker_create_argv` knobs: an attached cw session root
  gets the host docker socket and its ambient `CW_BRO` forwarded; a spawned child
  defaults to neither.
  """

  command: list[str]
  env: dict[str, str]
  secrets: Collection[str]
  attached: bool
  ring_bytes: int = DEFAULT_RING_BYTES
  bro: Optional[str] = None
  name: Optional[str] = None
  optional_secrets: Collection[str] = ()
  docker_sock: bool = False
  forward_bro: bool = False


@dataclass(frozen=True)
class ProcessLaunchSpec(LaunchSpec):
  """the concrete launch description `ProcessSpawner` reads: an interactive host
  subprocess run in `cwd` with inherited stdio (the launcher's TTY).

  `env` is the child's full environment — an explicit snapshot, never a live
  `os.environ` read (the same purity rule as `DockerLaunchSpec.env`); the spawner
  sets `BROKER_CHANNEL` on top, pointing at the provisioned socket.
  """

  command: list[str]
  cwd: str
  env: dict[str, str]


class _RingBuffer:
  """byte buffer retaining only the last `cap` bytes written."""

  def __init__(self, cap: int):
    if cap < 0:
      raise ValueError(f'ring buffer cap must be non-negative, got {cap}')
    self._cap = cap
    self._buf = bytearray()

  def write(self, data: bytes) -> None:
    self._buf += data
    overflow = len(self._buf) - self._cap
    if overflow > 0:
      del self._buf[:overflow]

  def tail(self) -> bytes:
    return bytes(self._buf)


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _broker_create_argv(
  launch: DockerLaunchSpec, host_socket: str, name: str, proj: Path, session: Path, tag: str
) -> list[str]:
  """`docker create` argv for a broker peer: the channel socket bind-mounted to
  `/run/broker.sock`, `BROKER_CHANNEL` pointed at it, and the bro-role (when set)
  stamped into `CW_BRO`. TTY allocation follows `launch.attached` — the root owns the
  host TTY; a supervised child is headless and gets no pty."""
  peer_env = dict(launch.env)
  peer_env['BROKER_CHANNEL'] = _BROKER_ADDRESS
  if launch.bro is not None:
    peer_env['CW_BRO'] = launch.bro
  return _docker_create_argv(
    tag,
    name,
    proj,
    session,
    launch.command,
    tty=launch.attached,
    docker_sock=launch.docker_sock,
    extra_env=peer_env,
    extra_mounts=[f'{host_socket}:{_IN_CONTAINER_SOCK}'],
    forward_bro=launch.forward_bro,
  )


async def _force_remove(container_id: str) -> None:
  # --rm removes the container on its process's exit; -f also covers a wedged one.
  # best-effort: a teardown race (already gone) is not an error.
  proc = await asyncio.create_subprocess_exec(
    'docker',
    'rm',
    '-f',
    container_id,
    stdout=asyncio.subprocess.DEVNULL,
    stderr=asyncio.subprocess.DEVNULL,
  )
  await proc.wait()


class _DockerChild(ChildHandle):
  def __init__(
    self,
    container_id: str,
    proc: asyncio.subprocess.Process,
    ring_bytes: int,
    workspace: Optional[ContainerWorkspace],
  ):
    self._container_id = container_id
    self._proc = proc
    self._ring = _RingBuffer(ring_bytes)
    self._drain = asyncio.create_task(self._drain_output())
    self._workspace = workspace  # a derived throwaway workspace, removed once the child ends

  async def _drain_output(self) -> None:
    assert self._proc.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._proc.stdout.read(_DRAIN_CHUNK)
      if len(chunk) == 0:
        return
      self._ring.write(chunk)

  async def _remove_workspace(self) -> None:
    # wait() and kill() can both get here (the timeout path kills, then the attach
    # exits); the swap happens before the first await, on the one loop, so the
    # workspace is removed exactly once. Best-effort: a child's dirs must never
    # break lifecycle routing.
    if self._workspace is None:
      return
    workspace, self._workspace = self._workspace, None
    try:
      # remove() shells out (image discovery, root-escalated rm); keep it off the loop
      await asyncio.to_thread(workspace.remove)
    except (RuntimeError, OSError) as e:
      log.warning('could not remove broker child workspace %s: %s', workspace.name, e)

  async def wait(self) -> int:
    code = await self._proc.wait()
    await self._drain  # let the final output land in the ring before tail() is read
    await self._remove_workspace()
    return code

  async def kill(self) -> None:
    await _force_remove(self._container_id)
    await self._remove_workspace()

  def output_tail(self) -> str:
    return self._ring.tail().decode('utf-8', errors='replace')


class _AttachedHandle(ChildHandle):
  """base handle for an interactive root: the child owns the inherited stdio.

  While attached, SIGINT is forwarded to the child rather than left to raise
  KeyboardInterrupt inside the event loop — an interrupt is meant for the session, and
  unwinding the loop would tear down the broker under it. The child and this process
  share the foreground process group (the child must keep reading the controlling
  TTY), so on a group-wide TTY interrupt the child may also receive the signal
  directly; the forward only matters for a signal targeted at the launcher. `wait()`
  restores default SIGINT handling once the child exits, then runs the subtype's
  `_on_exited` teardown. `output_tail()` is empty — the streams belong to the TTY."""

  def __init__(self, proc: asyncio.subprocess.Process):
    self._proc = proc
    self._loop = asyncio.get_running_loop()
    self._loop.add_signal_handler(signal.SIGINT, self._forward_sigint)

  def _forward_sigint(self) -> None:
    if self._proc.returncode is None:
      self._proc.send_signal(signal.SIGINT)

  async def wait(self) -> int:
    try:
      return await self._proc.wait()
    finally:
      self._loop.remove_signal_handler(signal.SIGINT)
      await self._on_exited()

  async def _on_exited(self) -> None:
    """subtype teardown, run after the child exits and before `wait()` returns."""

  def output_tail(self) -> str:
    return ''


class _AttachedRoot(_AttachedHandle):
  """handle for the interactive container root: the docker client owns the stdio."""

  def __init__(self, container_id: str, proc: asyncio.subprocess.Process):
    super().__init__(proc)
    self._container_id = container_id

  async def _on_exited(self) -> None:
    # a tty attach runs with docker's sig-proxy off, so the client can die (e.g. a
    # SIGINT targeted at it) while the container lives on. Client exit ends the
    # session either way; force-remove so the container follows — a no-op on the
    # normal path, where the container already exited and --rm is removing it.
    await _force_remove(self._container_id)

  async def kill(self) -> None:
    await _force_remove(self._container_id)


class _AttachedProcess(_AttachedHandle):
  """handle for the interactive host root: the child process itself owns the stdio."""

  async def kill(self) -> None:
    if self._proc.returncode is None:
      self._proc.kill()
      await self._proc.wait()


class DockerSpawner(Spawner):
  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, DockerLaunchSpec)
    proj = _project_root()
    name = launch.name if launch.name is not None else _workspace_name(channel.channel)
    session = _containers_dir(proj) / name
    session.mkdir(parents=True, exist_ok=True)
    tag = _image_tag()
    _ensure_image(tag)
    # strict: a missing required secret raises here, before the container is created.
    store = credentials.build_scoped_store(launch.secrets, optional=launch.optional_secrets)
    argv = _broker_create_argv(launch, str(channel.host_endpoint), name, proj, session, tag)
    container_id = _create_container(argv, _ppp_tarball(store), name)
    if launch.attached:
      # docker start -a -i with inherited stdio: the client owns the host TTY.
      proc = await asyncio.create_subprocess_exec('docker', 'start', '-a', '-i', container_id)
      return _AttachedRoot(container_id, proc)
    # docker start -a (no -i) attaches stdout+stderr to this subprocess, merged into one
    # pipe (chronological interleave) and drained by an async task into the ring.
    proc = await asyncio.create_subprocess_exec(
      'docker',
      'start',
      '-a',
      container_id,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    workspace = ContainerWorkspace(name, proj) if launch.name is None else None
    return _DockerChild(container_id, proc, launch.ring_bytes, workspace)


class ProcessSpawner(Spawner):
  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, ProcessLaunchSpec)
    env = dict(launch.env)
    env['BROKER_CHANNEL'] = f'unix:{channel.host_endpoint}'
    proc = await asyncio.create_subprocess_exec(*launch.command, cwd=launch.cwd, env=env)
    return _AttachedProcess(proc)


def run_root_via_broker(launch: LaunchSpec, spawner: Spawner, proj: Path) -> int:
  """run `launch` as the root peer of a broker over the host control dir
  (`var/cw/broker`), supervise it on the broker loop until it exits, and return its
  exit code. The broker answers the substrate's built-in ping, so a session can
  verify its channel (`broker request ping '{}'`); consumers register further
  request types on top."""
  facade = Broker(UnixServerTransport(str(_broker_dir(proj))), spawner)
  facade.on(Tag.PING, ping_handler)
  return facade.run(launch)
