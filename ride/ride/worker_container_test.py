import io
import tarfile
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import ride.worker_container as worker_container
from bro.broker.brotocol import Talk
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.worker_types import WorkerContainer
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver, Launch
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import DockerLaunchSpec


def _spec() -> WorkerContainer:
  return WorkerContainer(
    files={
      'Dockerfile': b'ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n',
      'worker/data.bin': b'payload',
    },
    command=('worker', '--serve'),
    env={'WORKER_MODE': 'test'},
    published_ports=(8080, 9090),
  )


class _Artifacts:
  ride = 'root'

  def __init__(self):
    self.views = []
    self.shares = []

  def view(self, workspace):
    self.views.append(workspace)

  def share(self, refs, *, to, by):
    self.shares.append((refs, to, by))


class _Facts:
  def __init__(self):
    self.workspaces = []
    self.ports = []

  def note_workspace(self, mission, workspace, *, artifact_view=None):
    self.workspaces.append((mission, workspace, artifact_view))

  def note_published_ports(self, mission, ports):
    self.ports.append((mission, ports))


def test_worker_image_build_streams_the_shipped_files_and_prunes(monkeypatch):
  calls = []
  pruned = []
  monkeypatch.setattr(worker_container, 'image_present', lambda tag: False)
  monkeypatch.setattr(
    worker_container.subprocess,
    'run',
    lambda arguments, **keywords: calls.append((arguments, keywords)) or MagicMock(returncode=0),
  )
  monkeypatch.setattr(
    worker_container,
    'prune_superseded_images',
    lambda tag: pruned.append(tag),
  )

  spec = _spec()
  tag = worker_container.ensure_worker_image('webview', 'bro/ride-runtime:abc', spec)

  [(arguments, keywords)] = calls
  assert tag == worker_container.worker_image_tag('webview', 'bro/ride-runtime:abc', spec)
  assert arguments == [
    'docker', 'build', '-t', tag,
    '--build-arg', 'RUNTIME_IMAGE=bro/ride-runtime:abc', '-',
  ]  # fmt: skip
  assert keywords['check'] is True
  with tarfile.open(fileobj=io.BytesIO(keywords['input']), mode='r:') as archive:
    assert archive.getnames() == sorted(spec.files)
    extracted = archive.extractfile('worker/data.bin')
    assert extracted is not None and extracted.read() == b'payload'
  assert pruned == [tag]


def test_present_worker_image_skips_build_and_prune(monkeypatch):
  monkeypatch.setattr(worker_container, 'image_present', lambda tag: True)
  monkeypatch.setattr(
    worker_container.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('present image was rebuilt'),
  )
  monkeypatch.setattr(
    worker_container,
    'prune_superseded_images',
    lambda tag: pytest.fail('present image triggered pruning'),
  )
  assert worker_container.ensure_worker_image(
    'webview', 'bro/ride-runtime:abc', _spec()
  ).startswith('bro/webview:')


def test_lowering_builds_a_detached_throwaway_launch(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  monkeypatch.setattr(
    worker_container,
    'ensure_worker_image',
    lambda worker_type, runtime_image, spec: 'bro/webview:image-hash',
  )
  monkeypatch.setattr(
    worker_container,
    '_published_ports',
    lambda ports: ((49152, ports[0]), (49153, ports[1])),
  )
  artifacts = _Artifacts()
  runtime = ContainerRuntimeResolver.fixed(
    ContainerRuntime('project-image', 'bundle-hash', 'runtime-image')
  )
  launch = worker_container.WorkerContainerLaunch(
    type='webview',
    spec=_spec(),
    owner_workspace='owner',
    share=('sha256:' + 'a' * 64,),
  )

  lowered = worker_container._lower_worker_container(
    launch,
    'webview-CH',
    runtime,
    cast(Any, artifacts),
  )

  assert lowered.launch == Launch(
    name='webview-CH',
    command=['broxy', 'run', '--', 'worker', '--serve'],
    env={
      'WORKER_MODE': 'test',
      'RIDE_PUBLISHED_PORTS': '8080=49152,9090=49153',
    },
    secrets=(),
    tty=False,
    image='bro/webview:image-hash',
    runtime_bundle_hash='bundle-hash',
    extra_mounts=(f'{tmp_path}/ride/artifacts/root/shared/webview-CH:/var/ride/artifacts:ro',),
    published_ports=((49152, 8080), (49153, 9090)),
  )
  workspace = Workspace.open('webview-CH')
  assert workspace.isolation is Isolation.BOXED
  assert workspace.repo is None
  assert workspace.metadata.throwaway
  assert artifacts.views == ['webview-CH']
  assert artifacts.shares == [
    (('sha256:' + 'a' * 64,), 'webview-CH', 'owner'),
  ]


@pytest.mark.asyncio
async def test_spawner_records_lowered_facts_and_delegates(monkeypatch):
  lowered = DockerLaunchSpec(
    Launch(
      name='webview-CH',
      command=['broxy', 'run', '--', 'worker'],
      env={'RIDE_PUBLISHED_PORTS': '8080=49152'},
      secrets=(),
      tty=False,
      image='bro/webview:hash',
      runtime_bundle_hash='bundle-hash',
      published_ports=((49152, 8080),),
    )
  )
  monkeypatch.setattr(worker_container, '_lower_worker_container', lambda *args: lowered)

  class _Docker:
    def __init__(self):
      self.calls = []

    async def spawn(self, launch, channel, mission, talk):
      self.calls.append((launch, channel, mission, talk))
      return MagicMock()

  docker = _Docker()
  facts = _Facts()
  spawner = worker_container.WorkerContainerSpawner(
    cast(Any, docker),
    ContainerRuntimeResolver.fixed(ContainerRuntime('runtime', 'bundle', 'runtime')),
    cast(Any, facts),
    cast(Any, _Artifacts()),
  )
  launch = worker_container.WorkerContainerLaunch(
    type='webview',
    spec=_spec(),
    owner_workspace='owner',
    share=(),
  )
  channel = Provisioned('CH', Endpoint(7321, 'token'))
  talk = cast(Talk, frozenset({'worker.say'}))

  child = await spawner.spawn(launch, channel, 'mission', talk)

  assert child is not None
  assert facts.workspaces == [('mission', 'webview-CH', True)]
  assert facts.ports == [('mission', ((49152, 8080),))]
  assert docker.calls == [(lowered, channel, 'mission', talk)]
