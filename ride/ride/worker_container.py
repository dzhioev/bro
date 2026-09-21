"""Host lowering for registered worker types that run in containers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import socket
import subprocess
import tarfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass

from bro.broker.brotocol import Talk
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned
from bro.worker_types import WorkerContainer
from ride.artifacts import ArtifactStore, view_mount
from ride.peer_facts import PeerFacts
from ride.workspace.docker import (
  ContainerRuntime,
  ContainerRuntimeResolver,
  Launch as DockerLaunch,
  image_present,
  prune_superseded_images,
)
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import DEFAULT_RING_BYTES, DockerLaunchSpec, DockerSpawner

_PUBLISHED_PORTS_ENV = 'RIDE_PUBLISHED_PORTS'
_IMAGE_LOCKS_GUARD = threading.Lock()
_IMAGE_LOCKS: dict[str, threading.Lock] = {}
_IMAGE_RESERVATIONS_GUARD = threading.Lock()
_IMAGE_RESERVATIONS: dict[str, int] = {}


@dataclass(frozen=True)
class WorkerContainerLaunch(LaunchSpec):
  type: str
  spec: WorkerContainer
  owner_workspace: str
  share: tuple[str, ...]


def _build_context(files: dict[str, bytes]) -> bytes:
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode='w') as archive:
    for path, content in sorted(files.items()):
      info = tarfile.TarInfo(path)
      info.size = len(content)
      info.mode = 0o644
      info.mtime = 0
      info.uid = 0
      info.gid = 0
      archive.addfile(info, io.BytesIO(content))
  return buffer.getvalue()


def worker_image_tag(worker_type: str, runtime_image: str, spec: WorkerContainer) -> str:
  return f'bro/{worker_type}:{spec.image_hash(runtime_image)}'


def _image_lock(tag: str) -> threading.Lock:
  with _IMAGE_LOCKS_GUARD:
    return _IMAGE_LOCKS.setdefault(tag, threading.Lock())


def _acquire_worker_image(tag: str) -> None:
  with _IMAGE_RESERVATIONS_GUARD:
    _IMAGE_RESERVATIONS[tag] = _IMAGE_RESERVATIONS.get(tag, 0) + 1


def _release_worker_image(tag: str) -> None:
  with _IMAGE_RESERVATIONS_GUARD:
    remaining = _IMAGE_RESERVATIONS[tag] - 1
    if remaining == 0:
      del _IMAGE_RESERVATIONS[tag]
    else:
      _IMAGE_RESERVATIONS[tag] = remaining


@contextlib.contextmanager
def _reserve_worker_image(tag: str) -> Iterator[None]:
  _acquire_worker_image(tag)
  try:
    yield
  finally:
    _release_worker_image(tag)


async def _wait_for_worker(operation: asyncio.Task[None]) -> bool:
  cancelled = False
  while True:
    try:
      await asyncio.shield(operation)
      return cancelled
    except asyncio.CancelledError:
      cancelled = True


async def _complete_worker_call(function: Callable[[str], None], tag: str) -> bool:
  operation = asyncio.create_task(asyncio.to_thread(function, tag))
  return await _wait_for_worker(operation)


@contextlib.asynccontextmanager
async def _reserve_worker_image_off_loop(tag: str) -> AsyncIterator[None]:
  acquisition = asyncio.create_task(asyncio.to_thread(_acquire_worker_image, tag))
  if await _wait_for_worker(acquisition):
    await _complete_worker_call(_release_worker_image, tag)
    raise asyncio.CancelledError
  try:
    yield
  finally:
    if await _complete_worker_call(_release_worker_image, tag):
      raise asyncio.CancelledError


def _prune_worker_images(tag: str) -> None:
  with _IMAGE_RESERVATIONS_GUARD:
    prune_superseded_images(tag, protected=_IMAGE_RESERVATIONS)


def ensure_worker_image(worker_type: str, runtime_image: str, spec: WorkerContainer) -> str:
  tag = worker_image_tag(worker_type, runtime_image, spec)
  with _reserve_worker_image(tag), _image_lock(tag):
    if image_present(tag):
      return tag
    built = subprocess.run(
      [
        'docker',
        'build',
        '-t',
        tag,
        '--build-arg',
        f'RUNTIME_IMAGE={runtime_image}',
        '-',
      ],
      input=_build_context(dict(spec.files)),
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
    )
    if built.stdout is None:
      raise RuntimeError(f'worker image build for {tag} returned no captured output')
    if built.returncode != 0:
      tail = built.stdout[-DEFAULT_RING_BYTES:].decode('utf-8', errors='replace').strip()
      detail = tail or '(no build output)'
      raise RuntimeError(
        f'worker image build for {tag} failed with exit code {built.returncode}:\n{detail}'
      )
    _prune_worker_images(tag)
  return tag


def _published_ports(container_ports: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
  selected = []
  with contextlib.ExitStack() as listeners:
    for container_port in container_ports:
      listener = listeners.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
      listener.bind(('127.0.0.1', 0))
      selected.append((listener.getsockname()[1], container_port))
  return tuple(selected)


def _lower_worker_container(
  launch: WorkerContainerLaunch,
  workspace_name: str,
  runtime: ContainerRuntime,
  image: str,
  artifacts: ArtifactStore,
) -> DockerLaunchSpec:
  ports = _published_ports(launch.spec.published_ports)
  workspace = Workspace.ensure(
    workspace_name,
    None,
    Isolation.BOXED,
    throwaway=True,
  )
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(workspace.remove)
    artifacts.view(workspace_name)
    artifacts.share(launch.share, to=workspace_name, by=launch.owner_workspace)
    published = ','.join(f'{container_port}={host_port}' for host_port, container_port in ports)
    lowered = DockerLaunchSpec(
      DockerLaunch(
        name=workspace_name,
        command=['broxy', 'run', '--', *launch.spec.command],
        env={**launch.spec.env, _PUBLISHED_PORTS_ENV: published},
        secrets=(),
        tty=False,
        image=image,
        runtime_bundle_hash=runtime.bundle_hash,
        extra_mounts=(view_mount(artifacts.ride, workspace_name, launch.spec.artifact_view),),
        published_ports=ports,
      )
    )
    cleanup.pop_all()
  return lowered


class WorkerContainerSpawner(Spawner):
  """Lower a core worker-container run off-loop and delegate it to Docker."""

  def __init__(
    self,
    docker: DockerSpawner,
    container_runtime: ContainerRuntimeResolver,
    facts: PeerFacts,
    artifacts: ArtifactStore,
  ):
    self._docker = docker
    self._container_runtime = container_runtime
    self._facts = facts
    self._artifacts = artifacts

  async def spawn(
    self,
    launch: LaunchSpec,
    channel: Provisioned,
    mission: str,
    talk: Talk,
  ) -> ChildHandle:
    assert isinstance(launch, WorkerContainerLaunch)
    workspace_name = f'{launch.type}-{channel.channel}'
    self._facts.note_workspace(
      mission,
      workspace_name,
      artifact_view=launch.spec.artifact_view,
    )
    runtime = await asyncio.to_thread(self._container_runtime.resolve)
    tag = worker_image_tag(launch.type, runtime.runtime_image, launch.spec)
    async with _reserve_worker_image_off_loop(tag):
      image = await asyncio.to_thread(
        ensure_worker_image,
        launch.type,
        runtime.runtime_image,
        launch.spec,
      )
      lowered = await asyncio.to_thread(
        _lower_worker_container,
        launch,
        workspace_name,
        runtime,
        image,
        self._artifacts,
      )
      self._facts.note_published_ports(mission, tuple(lowered.launch.published_ports))
      return await self._docker.spawn(lowered, channel, mission, talk)
