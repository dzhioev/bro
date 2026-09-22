import asyncio
import contextlib
import io
import subprocess
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import ride.worker_container as worker_container
from bro.broker.brotocol import Talk
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.worker_types import WorkerContainer
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
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
    published_ports=(8080, 9090),
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
    lambda tag, *, protected: pruned.append((tag, frozenset(protected))),
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
  assert pruned == [(tag, frozenset({tag}))]


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


def test_worker_image_builds_are_locked_per_tag_and_reserved_through_launch(monkeypatch):
  present: set[str] = set()
  build_calls: list[str] = []
  state_lock = threading.Lock()
  same_callers_ready = threading.Barrier(2)
  launches_ready = threading.Barrier(3)
  first_build_started = threading.Event()
  other_build_started = threading.Event()
  release_first_build = threading.Event()

  def image_present(tag):
    with state_lock:
      return tag in present

  def build(arguments, **keywords):
    tag = arguments[3]
    with state_lock:
      build_calls.append(tag)
    if arguments[5] == 'RUNTIME_IMAGE=bro/ride-runtime:one':
      first_build_started.set()
      if not release_first_build.wait(timeout=1):
        raise AssertionError('the independent image build did not start')
    else:
      other_build_started.set()
      release_first_build.set()
    with state_lock:
      present.add(tag)
    return subprocess.CompletedProcess(arguments, 0, stdout=b'built')

  def prune(tag, *, protected):
    with state_lock:
      for candidate in tuple(present):
        if candidate != tag and candidate not in protected:
          present.remove(candidate)

  def launch(runtime_image):
    tag = worker_container.worker_image_tag('webview-lock', runtime_image, _spec())
    with worker_container._reserve_worker_image(tag):
      if runtime_image == 'bro/ride-runtime:one':
        same_callers_ready.wait(timeout=1)
      image = worker_container.ensure_worker_image('webview-lock', runtime_image, _spec())
      launches_ready.wait(timeout=1)
      return image

  monkeypatch.setattr(worker_container, 'image_present', image_present)
  monkeypatch.setattr(worker_container.subprocess, 'run', build)
  monkeypatch.setattr(worker_container, 'prune_superseded_images', prune)

  with ThreadPoolExecutor(max_workers=3) as executor:
    first = executor.submit(launch, 'bro/ride-runtime:one')
    second = executor.submit(launch, 'bro/ride-runtime:one')
    assert first_build_started.wait(timeout=1)
    other = executor.submit(launch, 'bro/ride-runtime:two')
    try:
      assert other_build_started.wait(timeout=1)
    finally:
      release_first_build.set()
    assert first.result() == second.result()
    assert other.result() != first.result()

  assert build_calls.count(first.result()) == 1
  assert build_calls.count(other.result()) == 1
  assert present == {first.result(), other.result()}


@pytest.mark.asyncio
async def test_async_image_reservation_waits_off_the_event_loop(monkeypatch):
  acquire_started = threading.Event()
  release_acquire = threading.Event()
  reservation_entered = asyncio.Event()
  ticker_ran = asyncio.Event()
  acquire = worker_container._acquire_worker_image

  def blocking_acquire(tag):
    acquire_started.set()
    release_acquire.wait()
    acquire(tag)

  async def reserve():
    async with worker_container._reserve_worker_image_off_loop('bro/webview:waiting'):
      reservation_entered.set()

  async def tick():
    await asyncio.sleep(0)
    ticker_ran.set()

  monkeypatch.setattr(worker_container, '_acquire_worker_image', blocking_acquire)
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(release_acquire.set)
    reservation = asyncio.create_task(reserve())
    assert await asyncio.to_thread(acquire_started.wait, 1)
    ticker = asyncio.create_task(tick())
    await asyncio.wait_for(ticker_ran.wait(), timeout=1)
    assert not reservation_entered.is_set()
    release_acquire.set()
    await reservation
    await ticker

  assert worker_container._IMAGE_RESERVATIONS == {}


@pytest.mark.asyncio
async def test_cancelled_async_image_reservation_releases_a_late_acquisition(monkeypatch):
  acquire_started = threading.Event()
  release_acquire = threading.Event()
  reservation_entered = asyncio.Event()
  acquire = worker_container._acquire_worker_image

  def blocking_acquire(tag):
    acquire_started.set()
    release_acquire.wait()
    acquire(tag)

  async def reserve():
    async with worker_container._reserve_worker_image_off_loop('bro/webview:cancelled'):
      reservation_entered.set()

  monkeypatch.setattr(worker_container, '_acquire_worker_image', blocking_acquire)
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(release_acquire.set)
    reservation = asyncio.create_task(reserve())
    assert await asyncio.to_thread(acquire_started.wait, 1)
    reservation.cancel()
    await asyncio.sleep(0)
    assert not reservation.done()
    reservation.cancel()
    await asyncio.sleep(0)
    assert not reservation.done()
    release_acquire.set()
    with pytest.raises(asyncio.CancelledError):
      await reservation

  assert not reservation_entered.is_set()
  assert worker_container._IMAGE_RESERVATIONS == {}


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_async_image_reservation_release(monkeypatch):
  reservation_entered = asyncio.Event()
  release_started = threading.Event()
  allow_release = threading.Event()
  release = worker_container._release_worker_image

  def blocking_release(tag):
    release_started.set()
    allow_release.wait()
    release(tag)

  async def reserve():
    async with worker_container._reserve_worker_image_off_loop('bro/webview:cancelled'):
      reservation_entered.set()
      await asyncio.Future()

  monkeypatch.setattr(worker_container, '_release_worker_image', blocking_release)
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(allow_release.set)
    reservation = asyncio.create_task(reserve())
    await asyncio.wait_for(reservation_entered.wait(), timeout=1)
    reservation.cancel()
    assert await asyncio.to_thread(release_started.wait, 1)
    reservation.cancel()
    await asyncio.sleep(0)
    assert not reservation.done()
    allow_release.set()
    with pytest.raises(asyncio.CancelledError):
      await reservation

  assert worker_container._IMAGE_RESERVATIONS == {}


def test_lowering_builds_a_detached_throwaway_launch(monkeypatch, tmp_path):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
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
    spec=_spec(PurePosixPath('/workspace/shared')),
    owner_workspace='owner',
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
      assert len(worker_container._IMAGE_RESERVATIONS) == 1
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

  assert isinstance(child, worker_container._WorkerContainerChild)
  assert facts.workspaces == [('mission', 'webview-CH', PurePosixPath(CONTAINER_ARTIFACTS_ROOT))]
  assert facts.ports == [('mission', ((49152, 8080),))]
  assert docker.calls == [(lowered, channel, 'mission', talk)]
  assert worker_container._IMAGE_RESERVATIONS == {}
