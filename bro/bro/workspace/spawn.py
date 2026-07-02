"""DockerSpawner — cw's adapter for broker's `Spawner` port (non-TTY child mode).

Pairs with `UnixServerTransport`: the broker provisions a per-peer host socket, and
this spawner bind-mounts it into the child at the fixed `/run/broker.sock` and points
`BROKER_CHANNEL` at it, so the child's `broker` client connects back over the channel
the host supervises.

Two launch modes, keyed by `launch.attached` — stdio wiring only; supervision by the
broker's Runtime is uniform:

- Non-TTY (`attached == False`, a spawned child): `docker create` without `-it` (a
  headless child gets no pty) → `docker start -a` as an asyncio subprocess with
  stdout and stderr merged into one pipe and drained by an async task into a bounded
  ring buffer for `failed{output_tail}` — the streams are diagnostics for a child
  that dies without reporting; its result is channel-native (`completed{result}`).
  Attaching (rather than detaching + `docker wait` + `docker logs`) is deliberate:
  detached `--rm` removal races the log read.
- Interactive (`attached == True`, e.g. a cw session root): `docker create -it` →
  `docker start -a -i` as an asyncio subprocess with inherited stdio (the host TTY),
  plus a SIGINT forwarder — an interrupt of the launcher must reach the attached
  docker client, not unwind the broker loop out from under the running session.
  `output_tail()` is empty (the streams belong to the TTY).

Either way the exit code is the attached process's returncode and `kill()`
force-removes the container.
"""

import asyncio
import signal
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import credentials
from broker.spawn import ChildHandle, LaunchSpec, Spawner
from broker.transport import Provisioned
from cw.docker import _create_container, _docker_create_argv, _ensure_image, _image_tag
from cw.paths import _containers_dir, _project_root
from cw.secrets import _ppp_tarball

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
  derives a throwaway `broker-<channel>` one. `secrets` hydrate strictly,
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
  def __init__(self, container_id: str, proc: asyncio.subprocess.Process, ring_bytes: int):
    self._container_id = container_id
    self._proc = proc
    self._ring = _RingBuffer(ring_bytes)
    self._drain = asyncio.create_task(self._drain_output())

  async def _drain_output(self) -> None:
    assert self._proc.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._proc.stdout.read(_DRAIN_CHUNK)
      if len(chunk) == 0:
        return
      self._ring.write(chunk)

  async def wait(self) -> int:
    code = await self._proc.wait()
    await self._drain  # let the final output land in the ring before tail() is read
    return code

  async def kill(self) -> None:
    await _force_remove(self._container_id)

  def output_tail(self) -> str:
    return self._ring.tail().decode('utf-8', errors='replace')


class _AttachedRoot(ChildHandle):
  """handle for the interactive root: the docker client owns the inherited stdio.

  While attached, SIGINT is forwarded to the docker client rather than left to raise
  KeyboardInterrupt inside the event loop — an interrupt is meant for the session, and
  unwinding the loop would tear down the broker under it. The docker client and this
  process share the foreground process group (the client must keep reading the
  controlling TTY), so on a group-wide TTY interrupt the client may also receive the
  signal directly; the forward only matters for a signal targeted at the launcher.
  `wait()` restores default SIGINT handling once the client exits."""

  def __init__(self, container_id: str, proc: asyncio.subprocess.Process):
    self._container_id = container_id
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

  async def kill(self) -> None:
    await _force_remove(self._container_id)

  def output_tail(self) -> str:
    return ''


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
    return _DockerChild(container_id, proc, launch.ring_bytes)
