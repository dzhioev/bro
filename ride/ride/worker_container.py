"""Host lowering for registered worker types that run in containers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import socket
import subprocess
import tarfile
from dataclasses import dataclass

from bro.broker.brotocol import Talk
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned
from bro.worker_types import WorkerContainer
from ride.artifacts import ArtifactStore, view_mount
from ride.peer_facts import PeerFacts
from ride.workspace.docker import (
  ContainerRuntimeResolver,
  Launch as DockerLaunch,
  image_present,
  prune_superseded_images,
)
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import DockerLaunchSpec, DockerSpawner

_PUBLISHED_PORTS_ENV = 'RIDE_PUBLISHED_PORTS'


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


def ensure_worker_image(worker_type: str, runtime_image: str, spec: WorkerContainer) -> str:
  tag = worker_image_tag(worker_type, runtime_image, spec)
  if image_present(tag):
    return tag
  subprocess.run(
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
    check=True,
  )
  prune_superseded_images(tag)
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
  container_runtime: ContainerRuntimeResolver,
  artifacts: ArtifactStore,
) -> DockerLaunchSpec:
  runtime = container_runtime.resolve()
  image = ensure_worker_image(launch.type, runtime.runtime_image, launch.spec)
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
        extra_mounts=(view_mount(artifacts.ride, workspace_name),),
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
    self._facts.note_workspace(mission, workspace_name, artifact_view=True)
    lowered = await asyncio.to_thread(
      _lower_worker_container,
      launch,
      workspace_name,
      self._container_runtime,
      self._artifacts,
    )
    self._facts.note_published_ports(mission, tuple(lowered.launch.published_ports))
    return await self._docker.spawn(lowered, channel, mission, talk)
