import contextlib
import io
import json
import socket
import subprocess
import tarfile
from pathlib import PurePosixPath
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import ride.worker_container as worker_container
from bro.base import credentials
from bro.broker.brotocol import Talk
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.worker_types import WorkerContainer
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT, workspace_dir
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver, Launch
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import DockerLaunchSpec

_DEFAULT_ARTIFACT_VIEW = PurePosixPath(CONTAINER_ARTIFACTS_ROOT)


def _spec(
  artifact_view: PurePosixPath = _DEFAULT_ARTIFACT_VIEW,
) -> WorkerContainer:
  return WorkerContainer(
    files={
      'Dockerfile': b'ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n',
      'worker/data.bin': b'payload',
    },
    command=('worker', '--serve'),
    env={'WORKER_MODE': 'test'},
    published_ports={8080: None, 9090: None},
    artifact_view=artifact_view,
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
    lambda arguments, **keywords: (
      calls.append((arguments, keywords))
      or subprocess.CompletedProcess(arguments, 0, stdout=b'build output')
    ),
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
  assert keywords['stdout'] is subprocess.PIPE
  assert keywords['stderr'] is subprocess.STDOUT
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


def test_worker_image_build_can_skip_pruning(monkeypatch):
  monkeypatch.setattr(worker_container, 'image_present', lambda tag: False)
  monkeypatch.setattr(
    worker_container.subprocess,
    'run',
    lambda arguments, **keywords: subprocess.CompletedProcess(arguments, 0, stdout=b'built'),
  )
  monkeypatch.setattr(
    worker_container,
    'prune_superseded_images',
    lambda tag: pytest.fail(f'no-prune ensure pruned {tag}'),
  )

  worker_container.ensure_worker_image(
    'webview-no-prune',
    'bro/ride-runtime:abc',
    _spec(),
    prune=False,
  )


def test_failed_worker_image_build_reports_the_captured_output_tail(monkeypatch):
  marker = b'the actionable docker failure'
  output = b'old output' + b'x' * worker_container.DEFAULT_RING_BYTES + marker
  monkeypatch.setattr(worker_container, 'image_present', lambda tag: False)
  monkeypatch.setattr(
    worker_container.subprocess,
    'run',
    lambda arguments, **keywords: subprocess.CompletedProcess(
      arguments,
      17,
      stdout=output,
    ),
  )

  with pytest.raises(RuntimeError) as raised:
    worker_container.ensure_worker_image('webview-failure', 'bro/ride-runtime:abc', _spec())

  assert 'exit code 17' in str(raised.value)
  assert marker.decode() in str(raised.value)
  assert 'old output' not in str(raised.value)


def test_published_ports_keep_a_requested_host_port_and_select_the_rest():
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(('127.0.0.1', 0))
    requested = listener.getsockname()[1]

  ports = worker_container._published_ports({8080: requested, 9090: None})

  assert ports[0] == (requested, 8080)
  assert ports[1][1] == 9090
  assert ports[1][0] != requested


def test_requested_ports_are_reserved_before_automatic_selection(monkeypatch):
  requested_port = 40000
  alternate_port = 40001
  reserved: set[int] = set()

  class ControlledSocket:
    def __enter__(self):
      return self

    def __exit__(self, exception_type, exception, traceback):
      reserved.remove(self.port)

    def bind(self, address):
      port = address[1]
      if port == 0:
        port = requested_port if requested_port not in reserved else alternate_port
      if port in reserved:
        raise OSError(f'port {port} is already reserved')
      reserved.add(port)
      self.port = port

    def getsockname(self):
      return ('127.0.0.1', self.port)

  monkeypatch.setattr(
    worker_container.socket,
    'socket',
    lambda address_family, socket_type: ControlledSocket(),
  )

  ports = worker_container._published_ports({8080: None, 9090: requested_port})

  assert ports == ((alternate_port, 8080), (requested_port, 9090))
  assert reserved == set()


def test_lowering_builds_a_detached_throwaway_launch(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  monkeypatch.setattr(
    worker_container,
    '_published_ports',
    lambda ports: tuple(
      (49152 + index, container_port) for index, container_port in enumerate(ports)
    ),
  )
  artifacts = _Artifacts()
  runtime = ContainerRuntimeResolver.fixed(
    ContainerRuntime('project-image', 'bundle-hash', 'runtime-image')
  )
  launch = worker_container.WorkerContainerLaunch(
    type='webview',
    spec=_spec(PurePosixPath('/workspace/shared')),
    owner_workspace='owner',
    passes=(),
    share=('sha256:' + 'a' * 64,),
  )

  lowered = worker_container._lower_worker_container(
    launch,
    'webview-CH',
    runtime.resolve(),
    'bro/webview:image-hash',
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
    credential_store={'creds.json': b'{"defaults": [], "sources": {}}'},
    hydrated_kinds=frozenset(),
    extra_mounts=(f'{tmp_path}/ride/artifacts/root/shared/webview-CH:/workspace/shared:ro',),
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


def test_lowering_hydrates_the_passed_instances_into_the_container_store(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  monkeypatch.setattr('ride.workspace.model._cleanup_image', lambda repository: None)
  store = tmp_path / 'store'
  material = store / credentials.MATERIAL_DIR
  material.mkdir(parents=True)
  (material / f'github+work{credentials.MATERIAL_SUFFIX}').write_text('token')
  monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
  launch = worker_container.WorkerContainerLaunch(
    type='webview',
    spec=_spec(),
    owner_workspace='owner',
    passes=('github+work',),
    share=(),
  )
  runtime = ContainerRuntime('project-image', 'bundle-hash', 'runtime-image')

  lowered = worker_container._lower_worker_container(
    launch,
    'webview-passed',
    runtime,
    'bro/webview:image-hash',
    cast(Any, _Artifacts()),
  )

  with contextlib.ExitStack() as cleanup:
    workspace = Workspace.open('webview-passed')
    cleanup.callback(workspace.remove)
    assert lowered.launch.credential_store is not None
    assert lowered.launch.credential_store['creds/github+work.cred'] == b'token'
    assert json.loads(lowered.launch.credential_store['creds.json']) == {
      'defaults': ['github+work'],
      'sources': {},
    }
    assert lowered.launch.hydrated_kinds == frozenset({'github'})


def test_missing_pass_removes_the_throwaway_workspace(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))
  monkeypatch.setattr('ride.workspace.model._cleanup_image', lambda repository: None)
  store = tmp_path / 'store'
  store.mkdir()
  monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
  launch = worker_container.WorkerContainerLaunch(
    type='webview',
    spec=_spec(),
    owner_workspace='owner',
    passes=('github+missing',),
    share=(),
  )

  with pytest.raises(credentials.SecretNotFound) as raised:
    worker_container._lower_worker_container(
      launch,
      'webview-missing-pass',
      ContainerRuntime('project-image', 'bundle-hash', 'runtime-image'),
      'bro/webview:image-hash',
      cast(Any, _Artifacts()),
    )

  assert raised.value.name == 'github+missing'
  assert not workspace_dir('webview-missing-pass').exists()


@pytest.mark.parametrize('operation', ['wait', 'kill'])
@pytest.mark.asyncio
async def test_worker_child_prunes_its_empty_artifact_view_after_termination(
  operation, monkeypatch, tmp_path
):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  workspace = Workspace.create('webview-CH', None, Isolation.BOXED, throwaway=True)
  artifact_view = workspace.tree / 'mounts' / 'artifacts'
  artifact_view.mkdir(parents=True)
  retained = workspace.tree / 'retained'
  retained.mkdir()

  class _Child:
    def __init__(self):
      self.waited = False
      self.killed = False

    async def wait(self):
      self.waited = True
      return 3

    async def kill(self):
      self.killed = True

    def output_tail(self):
      return 'worker output'

  delegated = _Child()
  child = worker_container._WorkerContainerChild(
    cast(Any, delegated),
    workspace.name,
    PurePosixPath('/workspace/mounts/artifacts'),
  )

  result = await getattr(child, operation)()

  assert result == (3 if operation == 'wait' else None)
  assert delegated.waited is (operation == 'wait')
  assert delegated.killed is (operation == 'kill')
  assert child.output_tail() == 'worker output'
  assert not artifact_view.exists()
  assert not artifact_view.parent.exists()
  assert retained.is_dir()


def test_artifact_view_mount_cleanup_preserves_content(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  workspace = Workspace.create('webview-CH', None, Isolation.BOXED, throwaway=True)
  artifact_view = workspace.tree / 'artifacts'
  artifact_view.mkdir(parents=True)
  result = artifact_view / 'result.txt'
  result.write_text('keep')

  worker_container._remove_empty_artifact_view_mount(
    workspace.name, PurePosixPath('/workspace/artifacts')
  )

  assert result.read_text() == 'keep'


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
  monkeypatch.setattr(
    worker_container,
    'ensure_worker_image',
    lambda worker_type, runtime_image, spec: worker_container.worker_image_tag(
      worker_type, runtime_image, spec
    ),
  )

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
    passes=(),
    share=(),
  )
  channel = Provisioned('CH', Endpoint(7321, 'token'))
  talk = cast(Talk, frozenset({'worker.say'}))

  child = await spawner.spawn(launch, channel, 'mission', talk)

  assert isinstance(child, worker_container._WorkerContainerChild)
  assert facts.workspaces == [('mission', 'webview-CH', PurePosixPath(CONTAINER_ARTIFACTS_ROOT))]
  assert facts.ports == [('mission', ((49152, 8080),))]
  assert docker.calls == [(lowered, channel, 'mission', talk)]
