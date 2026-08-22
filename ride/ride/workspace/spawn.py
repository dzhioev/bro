"""broker spawner adapters for container and host-process launches.

`DockerSpawner` unwraps a broker-free `ride.workspace.docker.Launch`, adds
`BROKER_CHANNEL` (the provisioned channel under the container-facing host name)
and `BROKER_EXCHANGE`, and runs the shared blocking container prepare off-loop. A TTY root attaches with inherited stdio
and host-log redirection; a headless root inherits separate stdout and stderr;
a headless child captures merged output in a bounded ring and can remove its
workspace after a clean exit when the workspace records itself throwaway — a
failed or killed child's stays on disk for inspection. The neutral launch owns
the complete docker inputs, including the explicit env snapshot and whether
ambient forwarding is allowed.

`ProcessSpawner` runs a host-session root with inherited stdio, adds the
provisioned channel's loopback address and the exchange id directly to its
explicit environment, and applies the interactive signal and host-log handling only to
interactive launches.

`CompositeSpawner` dispatches on the concrete `LaunchSpec` type, so a broker
root of either mode can spawn children of any registered kind.
"""

import asyncio
import os
import signal
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from bro.base import log
from bro.broker.spawn import ChildHandle, LaunchSpec, RingBuffer, Spawner
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import LOCAL_HOST
from ride.workspace.docker import (
  CONTAINER_BROKER_HOST,
  DETACH_FLAG,
  Launch as DockerLaunch,
  container_running,
  prepare_container,
  suspend_until_continued,
)
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace

DEFAULT_RING_BYTES = 1 << 16  # 64 KiB — a full traceback + context, bounded

_DRAIN_CHUNK = 65536


@dataclass(frozen=True)
class DockerLaunchSpec(LaunchSpec):
  """broker adapter around a supervision-neutral container launch."""

  launch: DockerLaunch
  capture_output: bool = True
  ring_bytes: int = DEFAULT_RING_BYTES


@dataclass(frozen=True)
class ProcessLaunchSpec(LaunchSpec):
  """the concrete launch description `ProcessSpawner` reads: a host subprocess
  run in `cwd` with inherited stdio.

  `env` is the child's full environment — an explicit snapshot, never a live
  `os.environ` read (the same purity rule as `DockerLaunchSpec.env`); the spawner
  sets `BROKER_CHANNEL` and `BROKER_EXCHANGE` on top.
  """

  command: list[str]
  cwd: str
  env: dict[str, str]
  interactive: bool = True


def _broker_launch(launch: DockerLaunch, channel: Provisioned, exchange: str) -> DockerLaunch:
  """add the provisioned broker channel and the peer's exchange id to a neutral
  container launch."""
  env = dict(launch.env)
  env['BROKER_CHANNEL'] = channel.host_endpoint.address(CONTAINER_BROKER_HOST)
  env['BROKER_EXCHANGE'] = exchange
  return replace(launch, env=env)


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
    workspace: Optional[Workspace],
  ):
    self._container_id = container_id
    self._process = process
    self._ring = RingBuffer(ring_bytes)
    self._drain = asyncio.create_task(self._drain_output())
    self._workspace = workspace  # a derived throwaway workspace, removed on a clean exit

  async def _drain_output(self) -> None:
    assert self._process.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._process.stdout.read(_DRAIN_CHUNK)
      if len(chunk) == 0:
        return
      self._ring.write(chunk)

  def _detach_workspace(self) -> Optional[Workspace]:
    # wait() and kill() can both get here (the timeout path kills, then the attach
    # exits); the swap runs synchronously on the one loop, so the workspace is
    # settled exactly once — a kill's keep decision holds even when the attach
    # later exits 0.
    workspace, self._workspace = self._workspace, None
    return workspace

  async def _remove_workspace(self, workspace: Workspace) -> None:
    # best-effort: a child's dirs must never break lifecycle routing
    try:
      # remove() shells out (image discovery, root-escalated rm); keep it off the loop
      await asyncio.to_thread(workspace.remove)
    except (RuntimeError, OSError) as e:
      log.warning('could not remove broker child workspace %s: %s', workspace.name, e)

  async def wait(self) -> int:
    code = await self._process.wait()
    await self._drain  # let the final output land in the ring before tail() is read
    workspace = self._detach_workspace()
    if workspace is not None:
      if code == 0:
        await self._remove_workspace(workspace)
      else:
        workspace.record_session_end(code)
        log.info('child exited with code %d; keeping workspace %s', code, workspace.name)
    return code

  async def kill(self) -> None:
    await _force_remove(self._container_id)
    workspace = self._detach_workspace()
    if workspace is not None:
      workspace.record_session_end(None)
      log.info('child killed; keeping workspace %s', workspace.name)

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
    self._interrupt_forwarded = False
    self._loop = asyncio.get_running_loop()
    self._loop.add_signal_handler(signal.SIGINT, self._forward_sigint)
    self._redirect = _HostLogRedirect(host_log) if host_log is not None else None
    if self._redirect is not None:
      self._redirect.flip()

  def _forward_sigint(self) -> None:
    if self._process.returncode is None:
      self._interrupt_forwarded = True
      self._process.send_signal(signal.SIGINT)

  async def wait(self) -> int:
    try:
      return await self._wait_child()
    finally:
      self._loop.remove_signal_handler(signal.SIGINT)
      if self._redirect is not None:
        self._redirect.restore()
      await self._on_exited()

  async def _wait_child(self) -> int:
    return await self._process.wait()

  async def _on_exited(self) -> None:
    """subtype teardown, run after the child exits and before `wait()` returns."""

  def output_tail(self) -> str:
    return ''


class _AttachedRoot(_AttachedHandle):
  """handle for the interactive container root: the docker client owns the stdio.

  A client exit is ambiguous: the container may have exited (the client's code is its
  exit code), or the user hit Ctrl+Z — the client's detach key — and the container
  lives on. `_wait_child` tells them apart by the container's running state (the
  client exits 0 either way), suspends the session Ctrl+Z-style, and re-attaches on
  resume. A forwarded SIGINT also exits the client with 0, so ending the session on
  interrupt rides the remembered forward, not the exit code."""

  def __init__(
    self,
    container_id: str,
    process: asyncio.subprocess.Process,
    host_log: Optional[Path] = None,
  ):
    super().__init__(process, host_log)
    self._container_id = container_id

  async def _wait_child(self) -> int:
    while True:
      code = await self._process.wait()
      if code != 0 or self._interrupt_forwarded:
        return code
      if not await asyncio.to_thread(container_running, self._container_id):
        return code
      await asyncio.to_thread(suspend_until_continued, self._container_id)
      self._process = await asyncio.create_subprocess_exec(
        'docker', 'attach', DETACH_FLAG, self._container_id
      )

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


class _HeadlessProcess(ChildHandle):
  """handle for a non-interactive host root with inherited, separate streams."""

  def __init__(self, process: asyncio.subprocess.Process):
    self._process = process

  async def wait(self) -> int:
    return await self._process.wait()

  async def kill(self) -> None:
    if self._process.returncode is None:
      self._process.kill()
      await self._process.wait()

  def output_tail(self) -> str:
    return ''


class _HeadlessRoot(ChildHandle):
  """handle for a non-TTY container root with inherited, separate streams."""

  def __init__(self, container_id: str, process: asyncio.subprocess.Process):
    self._container_id = container_id
    self._process = process

  async def wait(self) -> int:
    code = await self._process.wait()
    await _force_remove(self._container_id)
    return code

  async def kill(self) -> None:
    await _force_remove(self._container_id)

  def output_tail(self) -> str:
    return ''


def _prepare_docker_spawn(
  launch: DockerLaunchSpec, channel: Provisioned, exchange: str
) -> tuple[str, Optional[Workspace]]:
  docker_launch = _broker_launch(launch.launch, channel, exchange)
  workspace = Workspace.ensure(docker_launch.name, docker_launch.repo, WorkspaceKind.CONTAINER)
  container_id = prepare_container(docker_launch)
  if not workspace.metadata.throwaway:
    return container_id, None
  workspace.clear_session_end()
  return container_id, workspace


class DockerSpawner(Spawner):
  def __init__(self, host_log: Optional[Path] = None):
    self._host_log = host_log

  async def spawn(self, launch: LaunchSpec, channel: Provisioned, exchange: str) -> ChildHandle:
    assert isinstance(launch, DockerLaunchSpec)
    container_id, workspace = await asyncio.to_thread(
      _prepare_docker_spawn, launch, channel, exchange
    )
    if launch.launch.tty:
      process = await asyncio.create_subprocess_exec(
        'docker', 'start', '-a', '-i', DETACH_FLAG, container_id
      )
      return _AttachedRoot(container_id, process, host_log=self._host_log)
    if not launch.capture_output:
      process = await asyncio.create_subprocess_exec('docker', 'start', '-a', container_id)
      return _HeadlessRoot(container_id, process)
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

  async def spawn(self, launch: LaunchSpec, channel: Provisioned, exchange: str) -> ChildHandle:
    assert isinstance(launch, ProcessLaunchSpec)
    env = dict(launch.env)
    env['BROKER_CHANNEL'] = channel.host_endpoint.address(LOCAL_HOST)
    env['BROKER_EXCHANGE'] = exchange
    process = await asyncio.create_subprocess_exec(*launch.command, cwd=launch.cwd, env=env)
    if launch.interactive:
      return _AttachedProcess(process, self._host_log)
    return _HeadlessProcess(process)


class CompositeSpawner(Spawner):
  """dispatch to the spawner registered for the concrete `LaunchSpec` type.

  the broker holds one spawner for the root and every spawned child alike, so a
  single-mode spawner would confine children to the root's launch mode; the
  composite lets a host-mode root (`ProcessLaunchSpec`) spawn docker children."""

  def __init__(self, spawners: dict[type[LaunchSpec], Spawner]):
    self._spawners = spawners

  async def spawn(self, launch: LaunchSpec, channel: Provisioned, exchange: str) -> ChildHandle:
    spawner = self._spawners.get(type(launch))
    if spawner is None:
      raise ValueError(f'no spawner registered for {type(launch).__name__}')
    return await spawner.spawn(launch, channel, exchange)
