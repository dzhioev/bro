"""live integration test of the broker-supervised container launch seam.

Drives the real launcher against the real docker daemon — the seam the fake
Transport/Spawner unit suites never touch. Host-only (it needs the host daemon,
so it is skipped inside a container), run as the gate's `broker_e2e` stage:

  run-tests --only broker_e2e   # or directly: pytest ride/ride/e2e_test.py [-k <scenario>]

The matrix: A — broker-enabled default launch (channel provisioning, the
entrypoint-owned broxy on the channel, ping round-trip through it, an artifact
mint/get through the read-only view mount); B — child
lifecycle over the real ports (spawn
routing, early exit, timeout, teardown, channel-pinned identity); C — the
`BROKER_DISABLED` kill-switch; D — degrade when broker is unimportable in the
launcher; E — SIGINT handling through the attached root; F — the in-place
session runner as the container command (exit-code propagation, in-container
argv build: merged --settings, MCP namespaces, RIDE_SESSION_CONTEXT);
G — the stop interrupt, so `docker stop` lands in claude as a keypress.

Isolation: every launch runs under a throwaway HOME, data home and project root,
so no scenario touches the user's own claude or runtime state. The
project root is a local git clone of this checkout. The launcher freezes the
current installation, materializes its container runtime volume, and resolves the
clone's project image before the scenarios start; the runtime volume serves the
`broker`/`bro` code to in-container probes.

Scenario containers synchronize with the harness through files on the shared
`/workspace` mount (`.e2e-ready` / `.e2e-continue` / `.e2e-report.json`) —
no tty parsing on the critical path.
"""

import json
import os
import pty
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

import bro.workspace.paths as workspace_paths
import ride.workspace.docker as workspace_docker
import ride.workspace.host_docker_test_helper as host_docker
from bro.broker.brotocol import Message
from bro.broker.dispatcher import Broker, Dispatcher, ping_handler, spawn_test_handler
from bro.broker.runtime import Peer
from bro.broker.transports.tcp import TcpServerTransport, parse_address
from ride.runtime_bundle import resolve_runtime_bundle
from ride.spawn import broker_bind_hosts
from ride.workspace.docker import CONTAINER_BROKER_HOST, find_container_id
from ride.workspace.spawn import DockerLaunchSpec, DockerSpawner

pytestmark = [
  *host_docker.HOST_DAEMON_ONLY,
  # a peer killed at broker teardown leaves its docker attach client un-awaited; the
  # subprocess transport's __del__ then runs after its loop closed and raises the benign
  # 'Event loop is closed', which GC surfaces in whatever test happens to run next
  pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning'),
]

_NAME_PREFIX = 'ride-e2e-'
_RUNTIME_PYTHON = '/var/ride/runtime/venv/bin/python'


# --- in-container probes (source for `python -c`; framework code comes from the runtime volume) ---

# scenario A root: verify the live channel (BROKER_CHANNEL rewritten by the
# entrypoint from the upstream host channel to its broxy's own loopback one),
# hand mid-run control to the harness, then run the ping round-trip over the
# exact live path — through the broxy — and an artifact mint/get proving the
# read-only view mount serves stored bytes back
_PROBE_A = """
import os, sys, time
from pathlib import Path

from bro.broker.transports.tcp import parse_address
channel = os.environ.get('BROKER_CHANNEL')
assert channel, 'the launch carried no channel'
assert parse_address(channel)[0] == '127.0.0.1', channel  # the broxy's, not the host's
assert os.environ.get('BROKER_EXCHANGE'), 'the launch did not carry the exchange id'
from bro.channel import BroChannel
channel = BroChannel.from_env()
assert channel is not None
channel.close()

Path('/workspace/.e2e-ready').touch()
deadline = time.monotonic() + 60
while not Path('/workspace/.e2e-continue').exists():
  if time.monotonic() > deadline:
    sys.exit(7)
  time.sleep(0.2)

from bro.broker.client import Client
client = Client.from_env()
assert client is not None
try:
  reply = client.request('ping', {'n': 1}, timeout=15)
except TimeoutError:
  print('RIDE_E2E_PING_TIMEOUT', flush=True)
  sys.exit(8)
assert reply.type == 'result', reply.type
assert reply.payload == {'outcome': 'ok', 'value': {'n': 1}}, reply.payload
print('RIDE_E2E_PING_OK', flush=True)

from bro.artifact import ArtifactError, get_artifact, mint_artifact
Path('/workspace/out.bin').write_bytes(b'artifact-payload')
try:
  minted = mint_artifact('out.bin', timeout=30)
  path = get_artifact(minted.ref, timeout=30)
except ArtifactError as error:
  print(f'RIDE_E2E_ARTIFACT_FAILED: {error}', flush=True)
  sys.exit(9)
content = Path(path).read_bytes()
assert content == b'artifact-payload', content
try:
  Path(path).write_bytes(b'overwrite')
except OSError:
  print('RIDE_E2E_ARTIFACT_OK', flush=True)
else:
  print('RIDE_E2E_ARTIFACT_WRITABLE', flush=True)
"""

# scenario B root: ping (with a forged identity claim in the payload), then spawn a
# child and record every message as received, wire-level, into a report file
_PROBE_B_ROOT = """
import json, os, sys, time, traceback
from pathlib import Path

report = {'messages': []}

def main():
  from bro.broker import brotocol
  from bro.broker.transport import connect

  deadline = float(os.environ['RIDE_E2E_DEADLINE'])
  exit_after = os.environ['RIDE_E2E_EXIT_AFTER']
  transport = connect(os.environ['BROKER_CHANNEL'])
  ping = brotocol.request('ping', {'n': 1, 'from': 'forged-peer-identity'})
  transport.send(ping)
  report['ping_id'] = ping.id
  reply = transport.receive(30)
  assert reply is not None, 'no ping reply'
  report['ping_reply'] = {
    'type': reply.type,
    'request': reply.request,
    'payload': reply.payload,
  }
  request = brotocol.request('spawn', {})
  start = time.monotonic()
  transport.send(request)
  report['request_id'] = request.id
  end = start + deadline
  while time.monotonic() < end:
    message = transport.receive(end - time.monotonic())
    if message is None:
      break
    report['messages'].append({
      'type': message.type,
      'request': message.request,
      'payload': message.payload,
      'elapsed': time.monotonic() - start,
    })
    if message.type == 'result':
      break
    if exit_after == 'started' and message.type == 'progress':
      break

try:
  main()
except BaseException:
  report['error'] = traceback.format_exc()
finally:
  Path('/workspace/.e2e-report.json').write_text(json.dumps(report))
sys.exit(1 if 'error' in report else 0)
"""

# scenario B clean child: emit the real lifecycle through the real consumer adapter
_CHILD_CLEAN = """
from bro.channel import BroChannel
channel = BroChannel.from_env()
assert channel is not None
channel.started('e2e-trail')
channel.completed('child-ok', 'ok')
channel.close()
"""

# scenario B early-exit child: die without reporting, leaving output on both streams
_CHILD_EARLY_EXIT = """
import sys
print('e2e-stdout-marker', flush=True)
print('e2e-stderr-marker', file=sys.stderr, flush=True)
sys.exit(3)
"""

# scenario B teardown child: attach, report started, then outlive the root
_CHILD_STARTED_THEN_HANG = """
import time
from bro.channel import BroChannel
channel = BroChannel.from_env()
assert channel is not None
channel.started('e2e-trail')
time.sleep(600)
"""

# scenarios C/D: assert the broker-less container surface
_PROBE_NO_CHANNEL = """
import os, subprocess, sys
from pathlib import Path

assert os.environ.get('BROKER_CHANNEL') is None, os.environ.get('BROKER_CHANNEL')
assert not Path('/run/broker.sock').exists()
from bro.channel import BroChannel
assert BroChannel.from_env() is None
inert = subprocess.run(['broker', 'send', 'ping', '{}'], capture_output=True, text=True)
assert inert.returncode == 0, (inert.returncode, inert.stderr)
assert 'request not sent' in inert.stderr, inert.stderr
print('RIDE_E2E_NO_CHANNEL_OK', flush=True)
"""

# scenario E root: catch SIGINT (delivered only via the container tty), else linger
_PROBE_SIGINT = """
import signal, sys, time
from pathlib import Path

def _handle(signum, frame):
  print('RIDE_E2E_SIGINT_CAUGHT', flush=True)
  sys.exit(0)

signal.signal(signal.SIGINT, _handle)
Path('/workspace/.e2e-ready').touch()
time.sleep(90)
sys.exit(5)
"""

# scenarios F/G: a wrapper the entrypoint execs in place of the session command.
# it drops a fake `claude` onto PATH — the in-place runner resolves it instead of
# the image's real one — then execs the runner itself ("$@", the same
# `ride solo|along --in-place …` invocation `_container_session` sends). the fake records
# its argv/env to the report file, proving the argv was built in-container by the
# frozen runtime; under RIDE_E2E_LINGER it waits for the interrupt keypress on its
# own terminal (exit 7) so the harness can assert `docker stop` reaches claude
# through tini → runner → the runner-owned pty.
_INPLACE_WRAPPER = """
mkdir -p /tmp/e2e-bin
cat > /tmp/e2e-bin/claude <<'FAKE'
#!/usr/bin/env python3
import json, os, signal, sys, tty
from pathlib import Path

report = {
  'argv': sys.argv[1:],
  'session_context_set': os.environ.get('RIDE_SESSION_CONTEXT') is not None,
}
argv = sys.argv[1:]
if '--settings' in argv:
  report['settings'] = json.loads(argv[argv.index('--settings') + 1])
Path('/workspace/.e2e-report.json').write_text(json.dumps(report))
if os.environ.get('RIDE_E2E_LINGER') == '1':
  # the fake stands in for a TUI that owns its terminal's mode; anything short of raw
  # leaves the pty's line discipline to eat the interrupt — as the intr character, or
  # as input canonical mode withholds until a line delimiter that never comes
  tty.setraw(0)
  signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(9))
  Path('/workspace/.e2e-ready').touch()
  sys.exit(7 if os.read(0, 1) == b'\\x03' else 8)
sys.exit(12)
FAKE
chmod +x /tmp/e2e-bin/claude
export PATH="/tmp/e2e-bin:$PATH"
exec "$@"
"""

# the launcher driver: one subprocess per live-path scenario, running the exact
# `ride solo|along` seam (`run_in_container`) under the isolated HOME/project root
_DRIVER = """
import json, os, sys
from ride.root import run_in_container
from ride.workspace.docker import Launch
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace
from bro.workspace.paths import project_root

name = os.environ['RIDE_E2E_NAME']
workspace = Workspace.ensure(name, project_root(), WorkspaceKind.CONTAINER)
claude_dir = workspace.path / 'claude'
session_dir = workspace.path / 'session'
claude_dir.mkdir()
session_dir.mkdir()
launch = Launch(name=name,
                command=json.loads(os.environ['RIDE_E2E_COMMAND']),
                env={'CLAUDE_CONFIG_DIR': '/home/ride/.claude',
                     'RIDE_SESSION_DIR': '/var/ride/session'},
                secrets=tuple(json.loads(os.environ.get('RIDE_E2E_SECRETS', '[]'))),
                tty=True, forward_env=True,
                image=os.environ['RIDE_E2E_IMAGE'],
                runtime_bundle_hash=os.environ['RIDE_E2E_RUNTIME_HASH'],
                extra_mounts=(f'{claude_dir}:/home/ride/.claude',
                              f'{session_dir}:/var/ride/session'),
                repo=project_root())
code = run_in_container(launch, workspace)
loaded = sorted(m for m in sys.modules if m == 'broker' or m.startswith('bro.broker.'))
print(f'RIDE_E2E_EXIT:{code}', flush=True)
print('RIDE_E2E_BROKER_MODULES:' + json.dumps(loaded), flush=True)
sys.exit(code)
"""


# --- harness -----------------------------------------------------------------


@dataclass(frozen=True)
class IsolatedEnv:
  root: Path
  project: Path
  home: Path
  data_home: Path
  runtime_root: Path
  image: str
  runtime_bundle_hash: str

  @property
  def workspaces_dir(self) -> Path:
    return self.runtime_root / 'workspaces'

  def tree(self, name: str) -> Path:
    return self.workspaces_dir / name / 'tree'

  def live_containers(self) -> list[str]:
    if not self.workspaces_dir.is_dir():
      return []
    live = []
    for workspace_dir in sorted(self.workspaces_dir.iterdir()):
      if find_container_id(self.tree(workspace_dir.name)) is not None:
        live.append(workspace_dir.name)
    return live

  def leaked_dirs(self, parent: Path) -> list[str]:
    if not parent.is_dir():
      return []
    return sorted(p.name for p in parent.iterdir() if p.name.startswith('broker-'))


def _remove_stray_containers(env: 'IsolatedEnv') -> None:
  if not env.workspaces_dir.is_dir():
    return
  for workspace_dir in env.workspaces_dir.iterdir():
    container_id = find_container_id(env.tree(workspace_dir.name))
    if container_id is not None:
      subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)


@pytest.fixture(scope='module')
def isolated_env() -> Iterator[IsolatedEnv]:
  with host_docker.scratch_root('broker-e2e') as root:
    project = root / 'project'
    subprocess.run(
      ['git', 'clone', '--quiet', str(host_docker.checkout()), str(project)], check=True
    )
    data_home = root / 'state'
    home = root / 'home'
    home.mkdir()
    # scenarios that exec the real runner hydrate `brog` (RIDE_E2E_SECRETS): the
    # session MCP server builds brog's backend from that secret at assembly, so
    # the health gate needs the stub a real session's scoped store would carry.
    # construction is offline — nothing contacts GitHub
    bro_dir = home / '.bro'
    credentials_dir = bro_dir / 'creds'
    credentials_dir.mkdir(parents=True)
    (credentials_dir / 'brog.cred').write_text(
      json.dumps(
        {
          'backend': 'github',
          'token': 'e2e',
          'repo': 'owner/repository',
        }
      )
    )
    (home / '.claude.json').write_text(
      json.dumps({'oauthAccount': {'emailAddress': 'e2e@invalid'}, 'userID': 'ride-e2e'})
    )
    (home / '.gitconfig').write_text(
      '[user]\n\tname = ride e2e\n\temail = e2e@invalid\n[init]\n\tdefaultBranch = master\n'
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
      monkeypatch.setenv('XDG_DATA_HOME', str(data_home))
      monkeypatch.setenv('DOCKER_HOST', host_docker.daemon_endpoint())
      runtime_root = workspace_paths.runtime_base()
      with resolve_runtime_bundle() as bundle:
        runtime = workspace_docker.ContainerRuntimeResolver(bundle, project).resolve()
        env = IsolatedEnv(
          root=root,
          project=project,
          home=home,
          data_home=data_home,
          runtime_root=runtime_root,
          image=runtime.image,
          runtime_bundle_hash=runtime.bundle_hash,
        )
        yield env
        _remove_stray_containers(env)


def _wait_until(
  predicate: Callable[[], bool], timeout: float, what: str, context: Optional[Callable[[], str]]
) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if predicate():
      return
    time.sleep(0.2)
  detail = f'\n--- context ---\n{context()}' if context is not None else ''
  pytest.fail(f'timed out after {timeout}s waiting for {what}{detail}')


def _poll_gone(predicate: Callable[[], bool], timeout: float) -> bool:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if predicate():
      return True
    time.sleep(0.5)
  return False


def _marker(output: str, name: str) -> Optional[str]:
  match = re.search(rf'{name}:(\S+)', output)
  if match is None:
    return None
  return match.group(1)


class _Driver:
  """the launcher subprocess under a pty (an attached session owns the terminal)."""

  def __init__(self, env: IsolatedEnv, name: str, command: list[str], extra_env: dict[str, str]):
    driver_env = {k: v for k, v in os.environ.items() if not k.startswith(('RIDE_', 'BROKER_'))}
    driver_env['HOME'] = str(env.home)
    driver_env['RIDE_E2E_NAME'] = name
    driver_env['RIDE_E2E_COMMAND'] = json.dumps(command)
    driver_env['RIDE_E2E_IMAGE'] = env.image
    driver_env['RIDE_E2E_RUNTIME_HASH'] = env.runtime_bundle_hash
    driver_env['XDG_DATA_HOME'] = str(env.data_home)
    driver_env.update(extra_env)
    master, slave = pty.openpty()
    self._master = master
    # -P keeps the driver's cwd (the isolated project clone, which carries its own copy
    # of every package) off sys.path: the launcher code under test must resolve from
    # this checkout's editable venv, and scenario D's PYTHONPATH shadow must win the
    # `import bro.broker` lookup
    self.process = subprocess.Popen(
      [sys.executable, '-P', '-c', _DRIVER],
      stdin=slave,
      stdout=slave,
      stderr=slave,
      cwd=env.project,
      env=driver_env,
      start_new_session=True,
    )
    os.close(slave)
    self._chunks: list[bytes] = []
    self._reader = threading.Thread(target=self._read, daemon=True)
    self._reader.start()

  def _read(self) -> None:
    while True:
      try:
        data = os.read(self._master, 65536)
      except OSError:  # EIO once every slave fd is closed
        return
      if len(data) == 0:
        return
      self._chunks.append(data)

  def output(self) -> str:
    return b''.join(self._chunks).decode('utf-8', errors='replace').replace('\r\n', '\n')

  def write(self, data: bytes) -> None:
    os.write(self._master, data)

  def wait(self, timeout: float) -> int:
    try:
      return self.process.wait(timeout)
    except subprocess.TimeoutExpired:
      pytest.fail(f'launcher driver still running after {timeout}s\n{self.output()}')

  def close(self) -> None:
    if self.process.poll() is None:
      try:
        os.killpg(self.process.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      self.process.wait(10)
    self._reader.join(5)
    os.close(self._master)


@dataclass
class LiveRun:
  """observations from one driver-launched live-path scenario."""

  exit_code: int
  output: str
  container_id: Optional[str] = None
  channel_address: Optional[str] = None  # BROKER_CHANNEL as the container was launched with it
  serving_after: Optional[bool] = None  # whether that channel's port still accepts
  container_gone_after: bool = False

  @property
  def reported_exit(self) -> Optional[str]:
    return _marker(self.output, 'RIDE_E2E_EXIT')

  @property
  def broker_modules(self) -> Optional[list[str]]:
    raw = _marker(self.output, 'RIDE_E2E_BROKER_MODULES')
    if raw is None:
      return None
    return json.loads(raw)


def _channel_env(container_id: str) -> Optional[str]:
  """the BROKER_CHANNEL the container was created with, or None for a broker-less
  launch."""
  inspect = subprocess.run(
    ['docker', 'inspect', '--format', '{{json .Config.Env}}', container_id],
    capture_output=True,
    text=True,
  )
  if inspect.returncode != 0:
    return None
  for entry in json.loads(inspect.stdout):
    key, _, value = entry.partition('=')
    if key == 'BROKER_CHANNEL':
      return value
  return None


def _accepts(address: str) -> bool:
  """whether the channel's port still accepts on this host — the container-facing
  name it was handed resolves nowhere here, so loopback stands in for it."""
  _, port, _ = parse_address(address)
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.settimeout(2)
    try:
      probe.connect(('127.0.0.1', port))
    except OSError:
      return False
  return True


def _wait_ready(env: IsolatedEnv, name: str, driver: _Driver, timeout: float = 240) -> None:
  ready = env.tree(name) / '.e2e-ready'
  _wait_until(
    lambda: ready.exists() or driver.process.poll() is not None,
    timeout,
    f'{name} probe ready file',
    driver.output,
  )
  if not ready.exists():
    pytest.fail(f'launcher exited before the {name} probe came up\n{driver.output()}')


def _container_gone(env: IsolatedEnv, name: str, timeout: float) -> bool:
  return _poll_gone(lambda: find_container_id(env.tree(name)) is None, timeout)


# --- A: broker-enabled default launch ----------------------------------------


@pytest.fixture(scope='module')
def scenario_a(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}a-root'
  driver = _Driver(env, name, [_RUNTIME_PYTHON, '-c', _PROBE_A], extra_env={})
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  run = LiveRun(exit_code=-1, output='')
  run.container_id = find_container_id(env.tree(name))
  if run.container_id is not None:
    run.channel_address = _channel_env(run.container_id)
  (env.tree(name) / '.e2e-continue').touch()
  run.exit_code = driver.wait(120)
  run.output = driver.output()
  if run.channel_address is not None:
    run.serving_after = _accepts(run.channel_address)
  run.container_gone_after = _container_gone(env, name, 15)
  return run


class TestBrokerEnabledLaunch:
  def test_channel_provisioned_under_the_container_facing_host(self, scenario_a: LiveRun) -> None:
    assert scenario_a.container_id is not None
    assert scenario_a.channel_address is not None
    host, port, token = parse_address(scenario_a.channel_address)
    assert host == CONTAINER_BROKER_HOST
    assert port > 0 and len(token) > 0

  def test_teardown_after_root_exit(self, scenario_a: LiveRun) -> None:
    assert scenario_a.serving_after is False, 'the channel still accepts after the root exited'
    assert scenario_a.container_gone_after, 'session container survived the root exit'

  def test_ping_round_trip_over_live_channel(self, scenario_a: LiveRun) -> None:
    assert 'RIDE_E2E_PING_OK' in scenario_a.output and scenario_a.exit_code == 0, (
      f'a ping over the live launch path got no correlated reply — the launch-path broker '
      f'refuses typed requests (driver exit {scenario_a.exit_code})\n{scenario_a.output}'
    )

  def test_artifact_round_trip_through_the_read_only_view(self, scenario_a: LiveRun) -> None:
    # mint over the live channel, read the ref back through the /var/ride/artifacts
    # bind, and prove the mount is kernel-enforced read-only
    assert 'RIDE_E2E_ARTIFACT_OK' in scenario_a.output and scenario_a.exit_code == 0, (
      f'the artifact round trip failed over the live launch path '
      f'(driver exit {scenario_a.exit_code})\n{scenario_a.output}'
    )


# --- B: child lifecycle over the real ports ----------------------------------


@dataclass
class BrokerRun:
  """observations from one in-process Broker run over the real transport + spawner."""

  code: int
  report: dict
  root_peer: Optional[Peer]
  observed_pings: list[tuple[Peer, dict]]
  max_channels: int
  max_live: int
  channels_after: frozenset[str]
  live_after: list[str]
  workspace_leaks: list[str]


def _run_broker_scenario(
  env: IsolatedEnv,
  case: str,
  child_command: list[str],
  *,
  default_timeout: float,
  probe_deadline: float,
  exit_after: str = 'ok',
  budget: float = 180,
) -> BrokerRun:
  name = f'{_NAME_PREFIX}b-{case}-root'
  root = DockerLaunchSpec(
    workspace_docker.Launch(
      name=name,
      command=[_RUNTIME_PYTHON, '-c', _PROBE_B_ROOT],
      env={'RIDE_E2E_DEADLINE': str(probe_deadline), 'RIDE_E2E_EXIT_AFTER': exit_after},
      secrets=(),
      tty=False,
      forward_env=False,
      image=env.image,
      runtime_bundle_hash=env.runtime_bundle_hash,
      repo=env.project,
    )
  )
  child = DockerLaunchSpec(
    workspace_docker.Launch(
      name=f'{name}-child',
      command=child_command,
      env={},
      secrets=(),
      tty=False,
      forward_env=False,
      image=env.image,
      runtime_bundle_hash=env.runtime_bundle_hash,
      repo=env.project,
    )
  )
  transport = TcpServerTransport(broker_bind_hosts())
  facade = Broker(transport, DockerSpawner(), default_timeout=default_timeout)
  observed_pings: list[tuple[Peer, dict]] = []

  def recording_ping(context: Dispatcher, peer: Peer, message: Message) -> None:
    observed_pings.append((peer, dict(message.args)))
    ping_handler(context, peer, message)

  facade.on('ping', recording_ping)
  facade.on('spawn', spawn_test_handler(child))

  result: dict[str, int] = {}
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setenv('HOME', str(env.home))
    thread = threading.Thread(target=lambda: result.update(code=facade.run(root)))
    thread.start()
    max_channels = 0
    max_live = 0
    deadline = time.monotonic() + budget
    while thread.is_alive() and time.monotonic() < deadline:
      max_channels = max(max_channels, len(transport.channels))
      max_live = max(max_live, len(env.live_containers()))
      time.sleep(0.25)
    if thread.is_alive():
      _remove_stray_containers(env)
      thread.join(30)
    if thread.is_alive():
      pytest.fail(f'broker run for case {case!r} wedged past {budget}s')

  report_path = env.tree(name) / '.e2e-report.json'
  report = json.loads(report_path.read_text()) if report_path.is_file() else {}
  return BrokerRun(
    code=result['code'],
    report=report,
    root_peer=facade._dispatcher._root,
    observed_pings=observed_pings,
    max_channels=max_channels,
    max_live=max_live,
    channels_after=transport.channels,
    live_after=env.live_containers(),
    workspace_leaks=env.leaked_dirs(env.workspaces_dir),
  )


@pytest.fixture(scope='module')
def b_clean(isolated_env: IsolatedEnv) -> BrokerRun:
  return _run_broker_scenario(
    isolated_env,
    'clean',
    [_RUNTIME_PYTHON, '-c', _CHILD_CLEAN],
    default_timeout=600,
    probe_deadline=120,
  )


@pytest.fixture(scope='module')
def b_early_exit(isolated_env: IsolatedEnv) -> BrokerRun:
  return _run_broker_scenario(
    isolated_env,
    'early',
    [_RUNTIME_PYTHON, '-c', _CHILD_EARLY_EXIT],
    default_timeout=600,
    probe_deadline=120,
  )


@pytest.fixture(scope='module')
def b_timeout(isolated_env: IsolatedEnv) -> BrokerRun:
  return _run_broker_scenario(
    isolated_env,
    'timeout',
    ['sleep', '300'],
    default_timeout=30,
    probe_deadline=90,
  )


@pytest.fixture(scope='module')
def b_teardown(isolated_env: IsolatedEnv) -> BrokerRun:
  return _run_broker_scenario(
    isolated_env,
    'teardown',
    [_RUNTIME_PYTHON, '-c', _CHILD_STARTED_THEN_HANG],
    default_timeout=600,
    probe_deadline=120,
    exit_after='started',
  )


class TestChildLifecycle:
  def test_ping_reply_and_channel_pinned_identity(self, b_clean: BrokerRun) -> None:
    assert 'error' not in b_clean.report, b_clean.report.get('error')
    reply = b_clean.report['ping_reply']
    assert reply['type'] == 'result'
    assert reply['request'] == b_clean.report['ping_id']
    assert reply['payload'] == {'outcome': 'ok', 'value': {'n': 1, 'from': 'forged-peer-identity'}}
    # the dispatcher attributed the request to the socket's own channel, not the
    # forged payload claim — identity is pinned to the channel the message arrived on
    assert b_clean.root_peer is not None
    assert b_clean.observed_pings == [(b_clean.root_peer, {'n': 1, 'from': 'forged-peer-identity'})]

  def test_child_lifecycle_routed_to_parent(self, b_clean: BrokerRun) -> None:
    assert b_clean.code == 0
    request_id = b_clean.report['request_id']
    types = [m['type'] for m in b_clean.report['messages']]
    assert types == ['progress', 'result'], b_clean.report['messages']
    started, completed = b_clean.report['messages']
    assert started['request'] == request_id
    assert started['payload'] == {'trail_id': 'e2e-trail'}
    assert completed['request'] == request_id
    assert completed['payload'] == {'outcome': 'ok', 'value': 'child-ok'}
    assert b_clean.max_channels == 2
    assert b_clean.max_live == 2
    assert b_clean.channels_after == frozenset()
    assert b_clean.live_after == []

  def test_no_workspace_dirs_leaked_after_parent_exit(self, b_clean: BrokerRun) -> None:
    assert b_clean.workspace_leaks == [], (
      'spawned child left workspace state behind after the parent exited: '
      f'{b_clean.workspace_leaks}'
    )

  def test_early_exit_child_synthesizes_failed(self, b_early_exit: BrokerRun) -> None:
    assert b_early_exit.code == 0
    types = [m['type'] for m in b_early_exit.report['messages']]
    assert types == ['result'], b_early_exit.report['messages']
    failed = b_early_exit.report['messages'][0]
    assert failed['request'] == b_early_exit.report['request_id']
    assert failed['payload']['outcome'] == 'failed'
    detail = failed['payload']['detail']
    assert detail['reason'] == 'exit'
    assert detail['exit_code'] == 3
    # stdout and stderr are merged into the one output tail
    assert 'e2e-stdout-marker' in detail['output_tail']
    assert 'e2e-stderr-marker' in detail['output_tail']
    assert b_early_exit.channels_after == frozenset()
    assert b_early_exit.live_after == []

  def test_wedged_child_times_out_at_default_timeout(self, b_timeout: BrokerRun) -> None:
    assert b_timeout.code == 0
    types = [m['type'] for m in b_timeout.report['messages']]
    assert types == ['result'], b_timeout.report['messages']
    failed = b_timeout.report['messages'][0]
    assert failed['request'] == b_timeout.report['request_id']
    assert failed['payload'] == {'outcome': 'failed', 'detail': {'reason': 'timeout'}}
    # the timer starts once the child is spawned, strictly after the request went out,
    # and fires at exactly default_timeout; the slack above covers the spawn overhead
    assert 30 <= failed['elapsed'] <= 60, failed['elapsed']
    assert b_timeout.channels_after == frozenset()
    assert b_timeout.live_after == [], 'timed-out child container not killed'

  def test_children_torn_down_on_root_exit(self, b_teardown: BrokerRun) -> None:
    assert b_teardown.code == 0
    types = [m['type'] for m in b_teardown.report['messages']]
    assert types == ['progress'], b_teardown.report['messages']
    assert b_teardown.channels_after == frozenset()
    assert b_teardown.live_after == [], 'live child container survived the root exit'


# --- C: BROKER_DISABLED kill-switch -------------------------------------------


@pytest.fixture(scope='module')
def scenario_c(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}c-root'
  driver = _Driver(
    env, name, [_RUNTIME_PYTHON, '-c', _PROBE_NO_CHANNEL], extra_env={'BROKER_DISABLED': '1'}
  )
  request.addfinalizer(driver.close)
  run = LiveRun(exit_code=-1, output='')
  run.exit_code = driver.wait(270)
  run.output = driver.output()
  return run


class TestKillSwitch:
  def test_session_launches_cleanly_without_broker(self, scenario_c: LiveRun) -> None:
    assert scenario_c.exit_code == 0, scenario_c.output
    assert 'RIDE_E2E_NO_CHANNEL_OK' in scenario_c.output

  def test_short_circuits_before_any_broker_import(self, scenario_c: LiveRun) -> None:
    assert scenario_c.broker_modules == []


# --- D: broker unimportable in the launcher -----------------------------------


# makes `import bro.broker` fail in the launcher. a module file cannot shadow a
# submodule of an installed package — the name resolves through the real
# `bro.__path__` and never consults PYTHONPATH — so the block is a meta-path
# finder, delivered through the `sitecustomize` that site imports from PYTHONPATH
# at interpreter start.
_BROKER_SHADOW = """
import sys


class _Unimportable:
  def find_spec(self, name, path=None, target=None):
    if name == 'bro.broker' or name.startswith('bro.broker.'):
      raise ImportError('shadowed for the degrade scenario')
    return None


sys.meta_path.insert(0, _Unimportable())
"""


@pytest.fixture(scope='module')
def scenario_d(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  shadow = env.root / 'shadow'
  shadow.mkdir(exist_ok=True)
  (shadow / 'sitecustomize.py').write_text(_BROKER_SHADOW)
  name = f'{_NAME_PREFIX}d-root'
  driver = _Driver(
    env, name, [_RUNTIME_PYTHON, '-c', _PROBE_NO_CHANNEL], extra_env={'PYTHONPATH': str(shadow)}
  )
  request.addfinalizer(driver.close)
  run = LiveRun(exit_code=-1, output='')
  run.exit_code = driver.wait(270)
  run.output = driver.output()
  return run


class TestBrokerUnimportable:
  def test_degrades_to_direct_launch_with_warning(self, scenario_d: LiveRun) -> None:
    assert scenario_d.exit_code == 0, scenario_d.output
    assert 'broker package not importable' in scenario_d.output
    assert 'RIDE_E2E_NO_CHANNEL_OK' in scenario_d.output
    assert scenario_d.broker_modules == []


# --- E: SIGINT through the attached root --------------------------------------


@pytest.fixture(scope='module')
def scenario_e_ctrl_c(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}e-ctrlc-root'
  driver = _Driver(env, name, [_RUNTIME_PYTHON, '-c', _PROBE_SIGINT], extra_env={})
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  container_id = find_container_id(env.tree(name))
  address = None if container_id is None else _channel_env(container_id)
  driver.write(b'\x03')  # ^C on the session terminal, forwarded raw into the container tty
  run = LiveRun(
    exit_code=driver.wait(60),
    output=driver.output(),
    container_id=container_id,
    channel_address=address,
  )
  run.serving_after = None if address is None else _accepts(address)
  run.container_gone_after = _container_gone(env, name, 15)
  return run


@pytest.fixture(scope='module')
def scenario_e_targeted(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}e-targeted-root'
  driver = _Driver(env, name, [_RUNTIME_PYTHON, '-c', _PROBE_SIGINT], extra_env={})
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  container_id = find_container_id(env.tree(name))
  address = None if container_id is None else _channel_env(container_id)
  os.kill(driver.process.pid, signal.SIGINT)  # targeted at the launcher, not the terminal group
  run = LiveRun(
    exit_code=driver.wait(60),
    output=driver.output(),
    container_id=container_id,
    channel_address=address,
  )
  run.serving_after = None if address is None else _accepts(address)
  run.container_gone_after = _container_gone(env, name, 15)
  if not run.container_gone_after and container_id is not None:
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)
  return run


class TestSigintHandling:
  def test_terminal_ctrl_c_clean_teardown(self, scenario_e_ctrl_c: LiveRun) -> None:
    assert 'RIDE_E2E_SIGINT_CAUGHT' in scenario_e_ctrl_c.output
    assert scenario_e_ctrl_c.exit_code == 0, scenario_e_ctrl_c.output
    assert 'Traceback' not in scenario_e_ctrl_c.output
    assert scenario_e_ctrl_c.serving_after is False
    assert scenario_e_ctrl_c.container_gone_after

  def test_targeted_sigint_does_not_unwind_the_loop(self, scenario_e_targeted: LiveRun) -> None:
    assert 'KeyboardInterrupt' not in scenario_e_targeted.output, scenario_e_targeted.output
    assert 'Traceback' not in scenario_e_targeted.output, scenario_e_targeted.output
    assert scenario_e_targeted.serving_after is False, 'teardown did not release the channel'

  def test_targeted_sigint_tears_down_the_container(self, scenario_e_targeted: LiveRun) -> None:
    assert scenario_e_targeted.container_gone_after, (
      'session container orphaned: the attach client died on the forwarded SIGINT but the '
      'container kept running — the launch must tear it down when the attached root dies'
    )


# --- F: the in-place session runner as the container command ------------------


def _inplace_command(*inner: str) -> list[str]:
  return ['bash', '-ec', _INPLACE_WRAPPER, 'ride-e2e-wrapper', *inner]


def _report(env: IsolatedEnv, name: str) -> dict:
  path = env.tree(name) / '.e2e-report.json'
  assert path.is_file(), f'no report from the fake claude at {path}'
  return json.loads(path.read_text())


@pytest.fixture(scope='module')
def scenario_f(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}f-root'
  # RIDE_BRO is set explicitly in the container env, as every launch surface does
  # for a persona-themed ride-session; the runner adapts that bro's spells
  driver = _Driver(
    env,
    name,
    _inplace_command(
      'ride',
      'solo',
      '--in-place',
      '--workspace',
      name,
      '--harness',
      'claude',
      '--repo',
      str(env.project),
      '--hold',
      'unattended',
      '--fast',
      'bro-dev',
      'e2e',
    ),
    extra_env={'RIDE_BRO': 'bro-dev', 'RIDE_E2E_SECRETS': '["brog"]'},
  )
  request.addfinalizer(driver.close)
  run = LiveRun(exit_code=driver.wait(300), output=driver.output())
  run.container_gone_after = _container_gone(env, name, 15)
  return run


class TestInPlaceContainerCommand:
  def test_claude_exit_code_propagates_to_the_launcher(self, scenario_f: LiveRun) -> None:
    # fake claude exits 12: runner → entrypoint → container → docker start → launcher
    assert scenario_f.reported_exit == '12', scenario_f.output
    assert scenario_f.exit_code == 12

  def test_argv_built_in_container(self, scenario_f: LiveRun, isolated_env: IsolatedEnv) -> None:
    report = _report(isolated_env, f'{_NAME_PREFIX}f-root')
    argv = report['argv']
    assert '--append-system-prompt' in argv, argv
    assert '--add-dir' not in argv
    mcp_config = json.loads(argv[argv.index('--mcp-config') + 1])
    assert 'brog' in mcp_config['mcpServers']
    assert report['session_context_set'] is True

  def test_fast_mode_reaches_the_merged_settings(
    self, scenario_f: LiveRun, isolated_env: IsolatedEnv
  ) -> None:
    report = _report(isolated_env, f'{_NAME_PREFIX}f-root')
    assert report['settings']['fastMode'] is True


# --- G: the stop interrupt — `docker stop` lands in claude ---------------------


@pytest.fixture(scope='module')
def scenario_g(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}g-root'
  driver = _Driver(
    env,
    name,
    _inplace_command(
      'env',
      'RIDE_E2E_LINGER=1',
      'ride',
      'solo',
      '--in-place',
      '--workspace',
      name,
      '--harness',
      'claude',
      '--repo',
      str(env.project),
      '--hold',
      'unattended',
      'bro-dev',
      'e2e',
    ),
    extra_env={'RIDE_E2E_SECRETS': '["brog"]'},
  )
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  container_id = find_container_id(env.tree(name))
  assert container_id is not None
  subprocess.run(['docker', 'stop', container_id], capture_output=True, check=True)
  run = LiveRun(exit_code=driver.wait(60), output=driver.output(), container_id=container_id)
  run.container_gone_after = _container_gone(env, name, 15)
  return run


class TestDockerStopReachesClaude:
  def test_the_interrupt_reaches_claude_and_the_exit_code_propagates(
    self, scenario_g: LiveRun
  ) -> None:
    # docker stop SIGTERMs pid 1 (tini), which forwards to the exec'd runner,
    # which types the interrupt into claude's terminal — the fake reads it and
    # exits 7. its SIGINT handler's 9 means the keypress never arrived, and a
    # SIGKILL after the grace period would surface as 137/143.
    assert scenario_g.reported_exit == '7', scenario_g.output
    assert scenario_g.exit_code == 7

  def test_container_torn_down(self, scenario_g: LiveRun) -> None:
    assert scenario_g.container_gone_after
