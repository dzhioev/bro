"""Host lowering for registered worker types that run in containers."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import socket
import subprocess
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from bro.base import credentials, log
from bro.broker.brotocol import Talk
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned
from bro.worker_types import WorkerContainer
from bro.workspace.paths import workspace_tree
from ride.artifacts import ArtifactStore, view_mount
from ride.peer_facts import PeerFacts
from ride.workspace.docker import (
  ContainerRuntime,
  ContainerRuntimeResolver,
  Launch as DockerLaunch,
  image_present,
  prune_superseded_images,
)
from ride.workspace.image_locks import ensure_image
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import DEFAULT_RING_BYTES, DockerLaunchSpec, DockerSpawner

_PUBLISHED_PORTS_ENV = 'RIDE_PUBLISHED_PORTS'


@dataclass(frozen=True)
class WorkerContainerLaunch(LaunchSpec):
  type: str
  spec: WorkerContainer
  owner_workspace: str
  passes: tuple[str, ...]
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


async def _wait_for_worker(operation: asyncio.Task[None]) -> bool:
  cancelled = False
  while True:
    try:
      await asyncio.shield(operation)
      return cancelled
    except asyncio.CancelledError:
      cancelled = True


def ensure_worker_image(
  worker_type: str,
  runtime_image: str,
  spec: WorkerContainer,
  *,
  prune: bool = True,
) -> str:
  tag = worker_image_tag(worker_type, runtime_image, spec)
  with ensure_image(tag):
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
    if prune:
      prune_superseded_images(tag)
  return tag


def _published_ports(
  requested_ports: Mapping[int, int | None],
) -> tuple[tuple[int, int], ...]:
  selected: dict[int, tuple[int, int]] = {}
  with contextlib.ExitStack() as listeners:
    for container_port, requested_host_port in requested_ports.items():
      if requested_host_port is None:
        continue
      listener = listeners.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
      listener.bind(('127.0.0.1', requested_host_port))
      selected[container_port] = (listener.getsockname()[1], container_port)
    for container_port, requested_host_port in requested_ports.items():
      if requested_host_port is not None:
        continue
      listener = listeners.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
      listener.bind(('127.0.0.1', 0))
      selected[container_port] = (listener.getsockname()[1], container_port)
  return tuple(selected[container_port] for container_port in requested_ports)


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
    source_store = credentials.Store(
      credentials.default_registry(), credentials.STORE_DIR, selection={}
    )
    credential_store, hydrated_kinds = credentials.build_scoped_store(source_store, launch.passes)
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
        credential_store=credential_store,
        hydrated_kinds=hydrated_kinds,
        extra_mounts=(view_mount(artifacts.ride, workspace_name, launch.spec.artifact_view),),
        published_ports=ports,
      )
    )
    cleanup.pop_all()
  return lowered


_CONTAINER_WORKSPACE = PurePosixPath('/workspace')


def _remove_empty_artifact_view_mount(workspace_name: str, artifact_view: PurePosixPath) -> None:
  try:
    relative_view = artifact_view.relative_to(_CONTAINER_WORKSPACE)
  except ValueError:
    return
  if not relative_view.parts:
    return
  tree = workspace_tree(workspace_name)
  path = tree.joinpath(*relative_view.parts)
  while path != tree:
    try:
      path.rmdir()
    except FileNotFoundError:
      pass
    except OSError as error:
      if error.errno in (errno.EEXIST, errno.ENOTDIR, errno.ENOTEMPTY):
        return
      log.warning('could not remove artifact-view mount point %s: %s', path, error)
      return
    path = path.parent


class _WorkerContainerChild(ChildHandle):
  def __init__(
    self,
    child: ChildHandle,
    workspace_name: str,
    artifact_view: PurePosixPath,
  ):
    self._child = child
    self._workspace_name = workspace_name
    self._artifact_view: PurePosixPath | None = artifact_view

  async def _remove_artifact_view_mount(self) -> None:
    artifact_view, self._artifact_view = self._artifact_view, None
    if artifact_view is None:
      return
    operation = asyncio.create_task(
      asyncio.to_thread(
        _remove_empty_artifact_view_mount,
        self._workspace_name,
        artifact_view,
      )
    )
    if await _wait_for_worker(operation):
      raise asyncio.CancelledError

  async def wait(self) -> int:
    code = await self._child.wait()
    await self._remove_artifact_view_mount()
    return code

  async def kill(self) -> None:
    await self._child.kill()
    await self._remove_artifact_view_mount()

  def output_tail(self) -> str:
    return self._child.output_tail()


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
    child = await self._docker.spawn(lowered, channel, mission, talk)
    return _WorkerContainerChild(child, workspace_name, launch.spec.artifact_view)
