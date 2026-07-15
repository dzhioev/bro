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
  The child's environment is the `LaunchSpec` snapshot only — the ambient
  `_DOCKER_FORWARD_ENV` forwards apply solely to the attached root (see
  `_broker_create_argv`).
- Interactive (`attached == True`, e.g. a cw session root): `docker create -it` →
  `docker start -a -i` as an asyncio subprocess with inherited stdio (the host TTY),
  plus a SIGINT forwarder — an interrupt of the launcher must reach the attached
  docker client, not unwind the broker loop out from under the running session.
  While the child owns the terminal, this process's own output is redirected to the
  session host log (`_HostLogRedirect`) so mid-session host lines and spawner
  shell-out chatter cannot paint over a raw-mode UI. `output_tail()` is empty (the
  streams belong to the TTY).

Either way the exit code is the attached process's returncode and `kill()`
force-removes the container.

`ProcessSpawner` is the host-mode sibling: the peer is a plain subprocess with
inherited stdio (the interactive root of a host session), and the provisioned socket
path is directly reachable from it, so `BROKER_CHANNEL` points straight at the host
endpoint — no bind-mount hop.

`run_root_via_broker` is the launch shape both modes share: one broker over the
`var/cw/broker` control dir, the `CompositeSpawner` over both launch modes plus the
summon lowering (so any root can spawn docker children), the built-in ping handler,
the root-lifecycle log handlers (the host process is the root's parent, so a root
run's `started`/`completed` land in the host log), and the root's `SummonControl`
(handler + delivery tap + teardown log, see `cw/summon.py`), supervise until the
root exits.

`SummonSpawner` turns an authorized summon into a docker child: `_lower_summon`
(off-loop) computes the launch a host-side `ask <target>` would get — the target's
own credential scope, `ask <target> "<prompt>"` non-TTY, based on the summoner's
workspace HEAD (or the request's `into` ref) via `CW_BASE_REF` — and delegates to
the docker path above.
"""

import asyncio
import os
import signal
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from base import credentials, log
from broker.brotocol import Message, Tag
from broker.dispatcher import Broker, Dispatcher, ping_handler
from broker.runtime import Peer
from broker.spawn import ChildHandle, LaunchSpec, Spawner
from broker.transport import Provisioned
from broker.transports.unix import UnixServerTransport
from cw.constants import bro_git_identity_env
from cw.docker import _create_container, _docker_create_argv, _ensure_image, _image_tag
from cw.git import resolve_head, resolve_ref
from cw.paths import _broker_dir, _containers_dir, _host_log_dir, _project_root, _summon_dir
from cw.secrets import Surface, _ppp_tarball, scoped_secrets
from cw.summon import SummonControl, summon_status_file
from cw.workspace import ContainerWorkspace
from summon import SUMMON

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
  this peer. Only an attached root gets the ambient `_DOCKER_FORWARD_ENV` forwards on
  top; a spawned child's environment is this snapshot alone (see
  `_broker_create_argv`). `bro` is the role stamped into `CW_BRO` (theming + secret
  scoping), `None` for a substrate-native peer with no bro.

  `name` is the workspace backing the container (`var/cw/containers/<name>`); `None`
  derives a throwaway `broker-<channel>` one, removed when the child ends. `secrets`
  hydrate strictly, `optional_secrets` best-effort (`credentials.build_scoped_store`).
  `docker_sock` and `forward_bro` mirror the `_docker_create_argv` knobs: an attached
  cw session root gets the host docker socket and its ambient `CW_BRO` forwarded; a
  spawned child defaults to neither.
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
class SummonLaunchSpec(LaunchSpec):
  """an authorized summon as a launch description, cheap to build on the broker
  loop: the request fields plus the summoner's workspace path (resolved by the
  control at request time — the source the child's default base is read from).
  `SummonSpawner` lowers it to a `DockerLaunchSpec` off-loop — the target-bro
  import, scoped-set computation, and base-ref resolution are all blocking work a
  handler must not run."""

  target: str
  prompt: str
  parent_workspace: Path
  into: Optional[str] = None


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
    self._buffer = bytearray()

  def write(self, data: bytes) -> None:
    self._buffer += data
    overflow = len(self._buffer) - self._cap
    if overflow > 0:
      del self._buffer[:overflow]

  def tail(self) -> bytes:
    return bytes(self._buffer)


def _workspace_name(channel: str) -> str:
  return f'broker-{channel}'


def _broker_create_argv(
  launch: DockerLaunchSpec, host_socket: str, name: str, project: Path, session: Path, tag: str
) -> list[str]:
  """`docker create` argv for a broker peer: the channel socket bind-mounted to
  `/run/broker.sock`, `BROKER_CHANNEL` pointed at it, and the bro-role (when set)
  stamped into `CW_BRO`. TTY allocation and ambient env forwarding both follow
  `launch.attached` — the root is the launcher's own session, so it owns the host
  TTY and inherits the `_DOCKER_FORWARD_ENV` set; a supervised child is headless
  and gets the `LaunchSpec` env snapshot only (forwarding would bake the
  launcher's values into it: mis-attributed commits, wrong banner facts)."""
  peer_env = dict(launch.env)
  peer_env['BROKER_CHANNEL'] = _BROKER_ADDRESS
  if launch.bro is not None:
    peer_env['CW_BRO'] = launch.bro
  return _docker_create_argv(
    tag,
    name,
    project,
    session,
    launch.command,
    tty=launch.attached,
    docker_sock=launch.docker_sock,
    extra_env=peer_env,
    extra_mounts=[f'{host_socket}:{_IN_CONTAINER_SOCK}'],
    forward_bro=launch.forward_bro,
    forward_env=launch.attached,
  )


async def _force_remove(container_id: str) -> None:
  # --rm removes the container on its process's exit; -f also covers a wedged one.
  # best-effort: a teardown race (already gone) is not an error.
  process = await asyncio.create_subprocess_exec(
    'docker',
    'rm',
    '-f',
    container_id,
    stdout=asyncio.subprocess.DEVNULL,
    stderr=asyncio.subprocess.DEVNULL,
  )
  await process.wait()


class _DockerChild(ChildHandle):
  def __init__(
    self,
    container_id: str,
    process: asyncio.subprocess.Process,
    ring_bytes: int,
    workspace: Optional[ContainerWorkspace],
  ):
    self._container_id = container_id
    self._process = process
    self._ring = _RingBuffer(ring_bytes)
    self._drain = asyncio.create_task(self._drain_output())
    self._workspace = workspace  # a derived throwaway workspace, removed once the child ends

  async def _drain_output(self) -> None:
    assert self._process.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._process.stdout.read(_DRAIN_CHUNK)
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
    code = await self._process.wait()
    await self._drain  # let the final output land in the ring before tail() is read
    await self._remove_workspace()
    return code

  async def kill(self) -> None:
    await _force_remove(self._container_id)
    await self._remove_workspace()

  def output_tail(self) -> str:
    return self._ring.tail().decode('utf-8', errors='replace')


class _HostLogRedirect:
  """point this process's stdout/stderr fds at the session host log while an
  interactive child owns the terminal.

  The child inherits the real terminal fds at spawn, so only this process's later
  output moves: mid-session host lines (summon lifecycle, broker warnings) and the
  inherited-fd output of spawner shell-outs (a mid-session `docker build` for a
  spawned child) land in the file instead of painting over the child's raw-mode UI.
  Both fds move because a shell-out is free to write progress to either. When
  stderr is not a terminal the flip is a no-op — headless consumers read those
  lines from stderr, and there is no screen to corrupt. When anything was written
  during the span, `restore()` points at it with one line (path + line count) on
  the restored terminal."""

  def __init__(self, path: Path):
    self._path = path
    self._saved: Optional[tuple[int, int]] = None
    self._file_fd = -1
    self._start_size = 0

  def flip(self) -> None:
    if not os.isatty(2):
      return
    sys.stdout.flush()
    sys.stderr.flush()
    self._path.parent.mkdir(parents=True, exist_ok=True)
    # kept open until restore(), and O_RDWR rather than O_WRONLY: the post-session
    # line count fstat+preads through this fd — valid even if the file is unlinked
    # meanwhile
    self._file_fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    self._start_size = os.fstat(self._file_fd).st_size
    self._saved = (os.dup(1), os.dup(2))
    os.dup2(self._file_fd, 1)
    os.dup2(self._file_fd, 2)

  def restore(self) -> None:
    if self._saved is None:
      return
    saved_stdout, saved_stderr = self._saved
    self._saved = None
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(saved_stdout, 1)
    os.dup2(saved_stderr, 2)
    os.close(saved_stdout)
    os.close(saved_stderr)
    appended = os.fstat(self._file_fd).st_size - self._start_size
    if appended > 0:
      line_count = os.pread(self._file_fd, appended, self._start_size).count(b'\n')
      noun = 'line' if line_count == 1 else 'lines'
      log.info('session host log: %s (%d %s this session)', self._path, line_count, noun)
    os.close(self._file_fd)
    self._file_fd = -1


class _AttachedHandle(ChildHandle):
  """base handle for an interactive root: the child owns the inherited stdio.

  While attached, SIGINT is forwarded to the child rather than left to raise
  KeyboardInterrupt inside the event loop — an interrupt is meant for the session, and
  unwinding the loop would tear down the broker under it. The child and this process
  share the foreground process group (the child must keep reading the controlling
  TTY), so on a group-wide TTY interrupt the child may also receive the signal
  directly; the forward only matters for a signal targeted at the launcher. For the
  same span — child spawn to child exit — host output is redirected to `host_log`
  (`_HostLogRedirect`). `wait()` restores default SIGINT handling and the real fds
  once the child exits, then runs the subtype's `_on_exited` teardown.
  `output_tail()` is empty — the streams belong to the TTY."""

  def __init__(self, process: asyncio.subprocess.Process, host_log: Optional[Path] = None):
    self._process = process
    self._loop = asyncio.get_running_loop()
    self._loop.add_signal_handler(signal.SIGINT, self._forward_sigint)
    self._redirect = _HostLogRedirect(host_log) if host_log is not None else None
    if self._redirect is not None:
      self._redirect.flip()

  def _forward_sigint(self) -> None:
    if self._process.returncode is None:
      self._process.send_signal(signal.SIGINT)

  async def wait(self) -> int:
    try:
      return await self._process.wait()
    finally:
      self._loop.remove_signal_handler(signal.SIGINT)
      if self._redirect is not None:
        self._redirect.restore()
      await self._on_exited()

  async def _on_exited(self) -> None:
    """subtype teardown, run after the child exits and before `wait()` returns."""

  def output_tail(self) -> str:
    return ''


class _AttachedRoot(_AttachedHandle):
  """handle for the interactive container root: the docker client owns the stdio."""

  def __init__(
    self,
    container_id: str,
    process: asyncio.subprocess.Process,
    host_log: Optional[Path] = None,
  ):
    super().__init__(process, host_log)
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
    if self._process.returncode is None:
      self._process.kill()
      await self._process.wait()


def _prepare_container(
  launch: DockerLaunchSpec, channel: Provisioned
) -> tuple[str, Optional[ContainerWorkspace]]:
  """the blocking prefix of a docker spawn: image ensure (a rebuild can take
  minutes), scoped-store build, `docker create` + store cp. Synchronous — `spawn`
  runs it via `asyncio.to_thread` so it never stalls the broker loop. Returns the
  created container id plus the derived throwaway workspace (None when the launch
  names a caller-owned one)."""
  project = _project_root()
  name = launch.name if launch.name is not None else _workspace_name(channel.channel)
  session = _containers_dir(project) / name
  session.mkdir(parents=True, exist_ok=True)
  tag = _image_tag()
  _ensure_image(tag)
  # strict: a missing required secret raises here, before the container is created.
  store = credentials.build_scoped_store(launch.secrets, optional=launch.optional_secrets)
  argv = _broker_create_argv(launch, str(channel.host_endpoint), name, project, session, tag)
  container_id = _create_container(argv, _ppp_tarball(store), name)
  workspace = ContainerWorkspace(name, project) if launch.name is None else None
  return container_id, workspace


class DockerSpawner(Spawner):
  def __init__(self, host_log: Optional[Path] = None):
    self._host_log = host_log

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, DockerLaunchSpec)
    container_id, workspace = await asyncio.to_thread(_prepare_container, launch, channel)
    if launch.attached:
      # docker start -a -i with inherited stdio: the client owns the host TTY.
      process = await asyncio.create_subprocess_exec('docker', 'start', '-a', '-i', container_id)
      return _AttachedRoot(container_id, process, host_log=self._host_log)
    # docker start -a (no -i) attaches stdout+stderr to this subprocess, merged into one
    # pipe (chronological interleave) and drained by an async task into the ring.
    process = await asyncio.create_subprocess_exec(
      'docker',
      'start',
      '-a',
      container_id,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    return _DockerChild(container_id, process, launch.ring_bytes, workspace)


class ProcessSpawner(Spawner):
  def __init__(self, host_log: Optional[Path] = None):
    self._host_log = host_log

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, ProcessLaunchSpec)
    env = dict(launch.env)
    env['BROKER_CHANNEL'] = f'unix:{channel.host_endpoint}'
    process = await asyncio.create_subprocess_exec(*launch.command, cwd=launch.cwd, env=env)
    return _AttachedProcess(process, self._host_log)


def _lower_summon(launch: SummonLaunchSpec) -> DockerLaunchSpec:
  """the blocking half of a summon spawn: compute the docker launch a host-side
  `ask <target>` would get. Privileges are exactly the target's own bro-run scope
  (`scoped_secrets` — nothing inherited from the summoner, no grant passthrough);
  the base is the summoner's workspace HEAD, read live here (`resolve_head` —
  which also transfers the commit's objects into the host repo when they live
  only in the summoner's own store), unless the request's `into` names a ref
  (resolved with the same fetch-if-unresolvable rule as `cw ss --into`, but an
  unresolvable ref fails the spawn rather than falling back). Raises on any
  unresolvable input — the spawner surfaces that as the correlated
  `failed{reason: 'launch'}`."""
  scoped = scoped_secrets(launch.target, Surface.BRO_RUN)
  project = _project_root()
  if launch.into is not None:
    base_ref = resolve_ref(project, launch.into)
    if base_ref is None:
      raise ValueError(f'cannot resolve summon into ref {launch.into!r}')
  else:
    base_ref = resolve_head(project, launch.parent_workspace)
    if base_ref is None:
      raise ValueError(f"cannot read the summoner's HEAD at {launch.parent_workspace}")
  return DockerLaunchSpec(
    # --host: the child is already the relayed, scoped run (do/CLAUDE.md)
    command=['ask', launch.target, launch.prompt, '--host'],
    env={'CW_BASE_REF': base_ref, **bro_git_identity_env()},
    secrets=scoped.required,
    attached=False,
    optional_secrets=scoped.optional,
    docker_sock=scoped.docker_sock,
  )


class SummonSpawner(Spawner):
  """lower a `SummonLaunchSpec` to its docker launch off-loop, then delegate to
  the docker path (which runs its own blocking prepare off-loop too)."""

  def __init__(self, docker: DockerSpawner):
    self._docker = docker

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, SummonLaunchSpec)
    lowered = await asyncio.to_thread(_lower_summon, launch)
    return await self._docker.spawn(lowered, channel)


class CompositeSpawner(Spawner):
  """dispatch to the spawner registered for the concrete `LaunchSpec` type.

  the broker holds one spawner for the root and every spawned child alike, so a
  single-mode spawner would confine children to the root's launch mode; the
  composite lets a host-mode root (`ProcessLaunchSpec`) spawn docker children."""

  def __init__(self, spawners: dict[type[LaunchSpec], Spawner]):
    self._spawners = spawners

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    spawner = self._spawners.get(type(launch))
    if spawner is None:
      raise ValueError(f'no spawner registered for {type(launch).__name__}')
    return await spawner.spawn(launch, channel)


def _log_root_started(context: Dispatcher, peer: Peer, message: Message) -> None:
  del context, peer
  log.info('root run started (trail %s)', message.payload.get('trail_id'))


def _log_root_completed(context: Dispatcher, peer: Peer, message: Message) -> None:
  del context, peer
  if message.payload.get('end_reason') == 'raised':
    # a raised run's result is the abort reason — surface it
    log.warning('root run raised: %s', message.payload.get('result'))
    return
  log.info('root run ended: %s', message.payload.get('end_reason'))


def run_root_via_broker(
  launch: LaunchSpec, project: Path, *, session: str, may_summon: Collection[str] = ()
) -> int:
  """run `launch` as the root peer of a broker over the host control dir
  (`var/cw/broker`), supervise it on the broker loop until it exits, and return its
  exit code. The spawner is the composite over both cw launch modes plus the summon
  lowering, so any root — host process or container — can spawn docker children.
  The broker answers the substrate's built-in ping, so a session can verify its
  channel (`broker request ping '{}'`), and logs the root's own run lifecycle
  (`started`/`completed`) as its parent. While an interactive root owns the
  terminal, host output goes to `var/cw/log/<session>.log` instead of the shared
  TTY (see `_HostLogRedirect`); headless runs keep it on stderr.

  `session` is the session key — the workspace name, mode-prefixed by the launch
  surface (see `cw/summon.py`) — the root's identity in the summon audit and the
  key of its per-session state files. `may_summon` names the bros the root session
  is authorized to summon — its effective outgoing allow-list (`cw/summon.py`);
  defaults to deny-all. A summoned child follows its own bro's static seeds
  instead, resolved per request by the control. The summon handler is registered
  either way, so a denied summoner always gets a clean correlated error instead of
  a silent refuse; after the loop ends — cleanly or by an exception unwinding out
  of it — children the root's exit killed mid-flight are logged loudly."""
  targets = sorted(set(may_summon))
  if len(targets) > 0:
    log.info('session may summon: %s', ', '.join(targets))
  host_log = _host_log_dir(project) / f'{session}.log'
  docker_spawner = DockerSpawner(host_log=host_log)
  spawner = CompositeSpawner(
    {
      DockerLaunchSpec: docker_spawner,
      ProcessLaunchSpec: ProcessSpawner(host_log=host_log),
      SummonLaunchSpec: SummonSpawner(docker_spawner),
    }
  )
  control = SummonControl(
    allow_list=may_summon,
    session=session,
    project=project,
    status_file=summon_status_file(project, session),
    audit_file=_summon_dir(project) / f'{session}.jsonl',
  )
  facade = Broker(UnixServerTransport(str(_broker_dir(project))), spawner)
  facade.on(Tag.PING, ping_handler)
  # the root's own lifecycle (a bro run at the session root) has no parent peer to
  # route to; this host process is its parent, so it lands in the host log
  facade.on(Tag.STARTED, _log_root_started)
  facade.on(Tag.COMPLETED, _log_root_completed)
  facade.on(SUMMON, control.handle)
  facade.add_delivery_observer(control.observe_delivery)
  try:
    return facade.run(launch)
  finally:
    control.log_killed_in_flight()
