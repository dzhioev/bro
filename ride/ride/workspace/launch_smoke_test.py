"""Live runtime-image launch and session provisioning checks."""

import contextlib
import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import ride.workspace.docker as workspace_docker
import ride.workspace.host_docker_test_helper as host_docker
from ride.repo_attachment import repository_attachment
from ride.runtime_bundle import resolve_runtime_bundle
from ride.workspace.docker import Launch
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace

pytestmark = host_docker.HOST_DAEMON_ONLY

_WORKSPACE_NAME = 'launch-smoke-test'
_READY_MARKER = '.launch-smoke-ready'
# the session command. it marks the shared workspace mount because a container is
# already `Running` while its entrypoint executes — only the command's own signal
# says the entrypoint handed over. then it outlives the observation, bounded so a
# container left behind by a crashed teardown still goes away on its own
_SESSION_COMMAND = [
  'bash',
  '-c',
  f'pwd > /tree/start-directory; touch ../beside-clone; touch /tree/{_READY_MARKER}; sleep 300',
]
_RUNNING_TIMEOUT = 120.0


@dataclass(frozen=True)
class Isolated:
  """the throwaway tree one launch runs against."""

  project: Path


@dataclass(frozen=True)
class Launched:
  """what one cold-tag launch showed."""

  tag: str
  image_before: bool
  image_after: bool
  running: bool
  clone: Path
  output: str
  runtime_bundle_hash: str


def _image_present(tag: str) -> bool:
  return subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0


@contextlib.contextmanager
def _cold_image(tag: str) -> Iterator[None]:
  """own the tag for the whole launch: absent going in, gone coming out."""
  subprocess.run(['docker', 'image', 'rm', '-f', tag], capture_output=True)
  try:
    yield
  finally:
    subprocess.run(['docker', 'image', 'rm', '-f', tag], capture_output=True)


def _wait_running(marker: Path, container_id: str, attached: subprocess.Popen[str]) -> bool:
  """whether the container is running once its session command has marked the
  workspace; False when the attached client exits first."""
  deadline = time.monotonic() + _RUNNING_TIMEOUT
  while not marker.is_file():
    if attached.poll() is not None:
      return False
    assert time.monotonic() < deadline, f'no {_READY_MARKER} within {_RUNNING_TIMEOUT:.0f}s'
    time.sleep(0.2)
  return workspace_docker.container_running(container_id)


def _drain(attached: subprocess.Popen[str]) -> str:
  try:
    return attached.communicate(timeout=30)[0]
  except subprocess.TimeoutExpired:
    attached.kill()
    return attached.communicate()[0]


@contextlib.contextmanager
def _running_container(container_id: str) -> Iterator[subprocess.Popen[str]]:
  """the prepared container's lifetime, attached so that one which dies inside its
  entrypoint leaves its output behind. removing it is what ends the attach, so the
  output is drained after this scope, not inside it."""
  attached = subprocess.Popen(
    ['docker', 'start', '-a', container_id],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  try:
    yield attached
  finally:
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)


def _start_and_observe(container_id: str, workspace: Path) -> tuple[bool, str]:
  with _running_container(container_id) as attached:
    running = _wait_running(workspace / _READY_MARKER, container_id, attached)
  return running, _drain(attached)


@pytest.fixture(scope='module')
def isolated() -> Iterator[Isolated]:
  """a standalone attachment clone inside the daemon's shared filesystem."""
  with host_docker.scratch_root('launch-smoke') as root:
    checkout = host_docker.checkout()
    project = root / 'project'
    subprocess.run(['git', 'clone', '--quiet', str(checkout), str(project)], check=True)
    origin = subprocess.run(
      ['git', '-C', str(checkout), 'remote', 'get-url', 'origin'],
      capture_output=True,
      text=True,
      check=True,
    )
    subprocess.run(
      ['git', '-C', str(project), 'remote', 'set-url', 'origin', origin.stdout.strip()], check=True
    )
    # a clone mirrors the source's local branches only, so seed a ref clone preparation refreshes
    subprocess.run(
      ['git', '-C', str(project), 'update-ref', 'refs/remotes/origin/master', 'HEAD'], check=True
    )
    yield Isolated(project=project)


@pytest.fixture(scope='module')
def launched(isolated: Isolated) -> Iterator[Launched]:
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setenv('XDG_DATA_HOME', str(isolated.project.parent / 'state'))
    monkeypatch.setattr(workspace_docker, '_RUNTIME_IMAGE_REPOSITORY', 'bro/launch-smoke-runtime')
    with resolve_runtime_bundle() as bundle:
      runtime_tag = workspace_docker.runtime_image_tag(bundle.python_version)
      with contextlib.ExitStack() as stack:
        stack.enter_context(_cold_image(runtime_tag))
        image_before = _image_present(runtime_tag)
        runtime = workspace_docker.ContainerRuntimeResolver(bundle).resolve()
        launch = Launch(
          name=_WORKSPACE_NAME,
          command=_SESSION_COMMAND,
          env={},
          secrets=(),
          tty=False,
          image=runtime.image,
          runtime_bundle_hash=runtime.bundle_hash,
          attachments=(repository_attachment(isolated.project, _WORKSPACE_NAME),),
        )
        recorded = Workspace.create(
          _WORKSPACE_NAME,
          (repository_attachment(isolated.project, _WORKSPACE_NAME),),
          Isolation.BOXED,
        )
        container_id = workspace_docker.prepare_container(launch)
        running, output = _start_and_observe(container_id, recorded.tree)
        yield Launched(
          tag=runtime_tag,
          image_before=image_before,
          image_after=_image_present(runtime_tag),
          running=running,
          clone=recorded.start_directory,
          output=output,
          runtime_bundle_hash=runtime.bundle_hash,
        )


def test_cold_runtime_image_is_built(launched: Launched) -> None:
  assert launched.image_before is False, f'{launched.tag} survived removal, so nothing was built'
  assert launched.image_after is True


def test_the_prepared_container_reaches_running_state(launched: Launched) -> None:
  assert launched.running is True, launched.output


def test_attached_launch_starts_in_the_clone_with_writable_tree_beside_it(launched):
  assert (
    launched.clone.parent / 'start-directory'
  ).read_text().strip() == f'/tree/{launched.clone.name}'
  assert (launched.clone.parent / 'beside-clone').is_file()


def test_the_workspace_mount_carries_an_independent_clone(launched: Launched) -> None:
  assert (launched.clone / '.git').is_dir(), launched.output
  assert not (launched.clone / '.git' / 'objects' / 'info' / 'alternates').exists(), launched.output


def test_detached_launch_starts_in_the_writable_tree(launched):
  workspace = Workspace.create('detached-launch-smoke', (), Isolation.BOXED)
  command = """
import json
import os
from pathlib import Path
Path('started.json').write_text(json.dumps({
  'directory': os.getcwd(),
  'host_tree': os.environ['RIDE_HOST_TREE'],
}))
"""
  launch = Launch(
    name=workspace.name,
    command=['/var/ride/runtime/venv/bin/python', '-c', command],
    env={},
    secrets=(),
    tty=False,
    image=launched.tag,
    runtime_bundle_hash=launched.runtime_bundle_hash,
    attachments=(),
  )
  container_id = workspace_docker.prepare_container(launch)
  with _running_container(container_id) as attached:
    output = attached.communicate(timeout=30)[0]
    assert attached.returncode == 0, output
  report = json.loads((workspace.tree / 'started.json').read_text())
  assert report == {'directory': '/tree', 'host_tree': str(workspace.tree)}
  assert list(workspace.tree.iterdir()) == [workspace.tree / 'started.json']


@pytest.mark.parametrize('name', ['bro', 'claude'])
def test_single_harness_image_starts_its_runtime(name, monkeypatch):
  import bro.harness as registry

  entries = tuple(entry for entry in registry._entry_points() if entry.name == name)
  assert len(entries) == 1
  monkeypatch.setattr(registry, '_entry_points', lambda: entries)
  monkeypatch.setattr(workspace_docker, '_RUNTIME_IMAGE_REPOSITORY', 'bro/single-harness-smoke')
  with host_docker.scratch_root('single-harness') as root:
    monkeypatch.setenv('XDG_DATA_HOME', str(root))
    with resolve_runtime_bundle() as bundle:
      tag = workspace_docker.runtime_image_tag(bundle.python_version)
      with _cold_image(tag):
        workspace_docker.ensure_runtime_image(tag, bundle.python_version, prune=False)
        bundle.materialize_container(tag, preflight=lambda: workspace_docker._preflight_daemon(tag))
        command = (
          'test ! -e /opt/claude-code; bro --help'
          if name == 'bro'
          else '/opt/claude-code/claude --version'
        )
        subprocess.run(
          [
            'docker',
            'run',
            '--rm',
            '-v',
            f'{bundle.container_volume}:/var/ride/runtime:ro',
            tag,
            'bash',
            '-ec',
            command,
          ],
          check=True,
        )


_PROVISIONING_SESSION = """
import os
from pathlib import Path
from types import SimpleNamespace
from bro.base import credentials
from ride import do_ride
import ride.repo_attachment as repository_attachment
from ride.attachments import Attachment

root = Path('/tree') / 'repository'
state = Path('/home/ride/session')
state.mkdir()
os.chdir(root)
os.environ.update(RIDE_ISOLATION='boxed', RIDE_SESSION_DIR=str(state))
store_directory = Path('/home/ride/provision-store')
(store_directory / 'creds').mkdir(parents=True)
(store_directory / 'creds' / 'probe.cred').write_text('selected-session')
registry = {'probe': credentials.CredentialKind('probe', 'probe identity', install={
  'files': {'token': {'secret': 'probe'}},
  'env': {'TOKEN_FILE': {'path': 'token'}},
  'commands': {'printenv': {'env': {'PROBE_PATH': {'path': 'token'}}}},
})}
store = credentials.Store(registry, store_directory, {})
credentials.default_store = lambda: store
os.environ.update(BRO_STORE=str(store_directory), BRO_INSTALL_KINDS='probe')

def persona(path):
  if os.environ['SCENARIO'] == 'declared':
    assert (path / 'provisioned').read_text() == ('selected-session\\n' * len(starts))
  (path / 'persona').touch()

def prepare(run):
  assert (root / 'persona').is_file()

def harness(run):
  print('session reply', flush=True)
  return 0

repository_attachment.create_bro = lambda name: SimpleNamespace(provision_workspace=persona)
starts = []
for resume in (False, True):
  starts.append(resume)
  run = do_ride.SessionRun(
    name='provision-smoke', attachments=(Attachment('repo', 'repository', {'repo': '/host/repo', 'branch': 'work'}),), harness='bro', hold='unattended',
    resolved_recipe={}, solo=True, resume=resume, bro='bro', prompt='probe',
    activity_file=state / 'activity',
  )
  code = do_ride.run_session(SimpleNamespace(prepare_session=prepare, run_session=harness), run)
  if code:
    raise SystemExit(code)
"""


@pytest.mark.parametrize('scenario', ['declared', 'undeclared', 'failed', 'unknown'])
def test_boxed_session_provisioning_at_launch_and_resume(launched, isolated, scenario):
  tree = isolated.project.parent / f'provision-{scenario}'
  tree.mkdir()
  directory = tree / 'repository'
  directory.mkdir()
  config = directory / '.bro' / 'config.toml'
  config.parent.mkdir()
  command = (
    'test -n "$RIDE_RUNNER_PID" && test "$BRO_HOLD" = unattended && '
    '! read -r answer && '
    'test "$(printenv PROBE_PATH)" = "$TOKEN_FILE" && '
    'cat "$TOKEN_FILE" >> provisioned && printf "\\n" >> provisioned && '
    'printf "provision output\\n" && printf "provision error\\n" >&2'
  )
  if scenario == 'declared':
    config.write_text(f'provision-command = {json.dumps(command)}\n')
  elif scenario == 'failed':
    config.write_text('provision-command = "exit 23"\n')
  elif scenario == 'unknown':
    config.write_text('unknown = true\n')
  script = directory / 'setup.sh'
  script.write_text('#!/bin/sh\ntouch filename-hook-ran\nexit 99\n')
  script.chmod(0o755)
  result = subprocess.run(
    [
      'docker',
      'run',
      '--rm',
      '-v',
      f'{tree}:/tree',
      '-v',
      f'ride-runtime-{launched.runtime_bundle_hash}:/var/ride/runtime:ro',
      '-e',
      f'SCENARIO={scenario}',
      launched.tag,
      '/var/ride/runtime/venv/bin/python',
      '-c',
      _PROVISIONING_SESSION,
    ],
    capture_output=True,
    text=True,
  )
  assert not (directory / 'filename-hook-ran').exists()
  if scenario in ('failed', 'unknown'):
    assert result.returncode == 1, result.stderr
    assert result.stdout == ''
    assert not (directory / 'persona').exists()
    if scenario == 'failed':
      assert 'provisioning command failed with exit status 23: exit 23' in result.stderr
    else:
      assert '.bro/config.toml' in result.stderr and 'unknown' in result.stderr
  else:
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'session reply\nsession reply\n'
    if scenario == 'declared':
      assert (directory / 'provisioned').read_text() == 'selected-session\nselected-session\n'
      assert result.stderr.count('provision output') == 2
      assert result.stderr.count('provision error') == 2
