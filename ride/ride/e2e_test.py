"""live integration test of the broker-supervised container launch seam.

Drives the real launcher against the real docker daemon — the seam the fake
Transport/Spawner unit suites never touch. Host- and linux-only (needs the host
daemon; skipped inside a container, and on macOS, where container sessions are
pinned to the broker-less path — see `_container_broker_enabled`) and, like
every live integration test, run separately from the default suite:

  pytest ride/e2e_test.py [-k <scenario>]

The matrix: A — broker-enabled default launch (socket provisioning, the
entrypoint-owned broxy on the channel, ping round-trip through it); B — child
lifecycle over the real ports (spawn
routing, early exit, timeout, teardown, channel-pinned identity); C — the
`BROKER_DISABLED` kill-switch; D — degrade when broker is unimportable in the
launcher; E — SIGINT handling through the attached root; F — the in-place
session runner as the container command (exit-code propagation, in-container
argv build: merged --settings, MCP namespaces, RIDE_SESSION_CONTEXT);
G — SIGTERM forwarding, so `docker stop` lands in claude.

Isolation: every launch runs under a throwaway HOME, data home and project root,
so no scenario touches the user's own claude or runtime state. The
project root is a local git clone of this checkout — its `pyproject.toml` /
`uv.lock` are byte-identical, so `_image_tag()` resolves to the already-built
image, and the container's baked venv serves this branch's committed
`broker`/`bro` code to the in-container probes (launcher code comes from this
checkout's editable venv, uncommitted changes included). The tree lives under
a short `mkdtemp` dir directly in the system temp dir: the broker socket path
must fit `sun_path` (~108 bytes), which rules out deeper roots.

Scenario containers synchronize with the harness through files on the shared
`/workspace` mount (`.e2e-ready` / `.e2e-continue` / `.e2e-report.json`) —
no tty parsing on the critical path.
"""

import json
import os
import pty
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

import bro.workspace.paths as workspace_paths
import ride.spawn
import ride.workspace.docker as workspace_docker
import ride.workspace.spawn as workspace_spawn
from bro.broker.brotocol import Message
from bro.broker.dispatcher import Broker, Dispatcher, ping_handler, spawn_test_handler
from bro.broker.runtime import Peer
from bro.broker.transports.unix import UnixServerTransport
from ride.workspace.docker import find_container_id
from ride.workspace.spawn import DockerLaunchSpec, DockerSpawner


def _docker_available() -> bool:
  try:
    return subprocess.run(['docker', 'info'], capture_output=True).returncode == 0
  except FileNotFoundError:
    return False


pytestmark = [
  pytest.mark.skipif(
    Path('/.dockerenv').is_file(), reason='host-only: drives the host docker daemon'
  ),
  pytest.mark.skipif(not _docker_available(), reason='no reachable docker daemon'),
  # the seam under test is linux-only: a VM-backed daemon can't bind-mount the host
  # channel socket, so `_container_broker_enabled` pins macOS to the broker-less path
  pytest.mark.skipif(sys.platform == 'darwin', reason='broker container seam is linux-only'),
  # a peer killed at broker teardown leaves its docker attach client un-awaited; the
  # subprocess transport's __del__ then runs after its loop closed and raises the benign
  # 'Event loop is closed', which GC surfaces in whatever test happens to run next
  pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning'),
]

_NAME_PREFIX = 'ride-e2e-'


# --- in-container probes (source for `python -c`; repo code comes from the baked venv) ---

# scenario A root: verify the live channel (BROKER_CHANNEL rewritten by the
# entrypoint to its broxy's local socket, the upstream socket bind-mounted
# next to it), hand mid-run control to the harness, then run the ping
# round-trip over the exact live path — through the broxy
_PROBE_A = """
import os, sys, time
from pathlib import Path

assert os.environ.get('BROKER_CHANNEL') == 'unix:/tmp/broxy.sock', os.environ.get('BROKER_CHANNEL')
assert Path('/run/broker.sock').is_socket()
assert Path('/tmp/broxy.sock').is_socket()
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
assert reply.type == 'reply', reply.type
assert reply.payload == {'pong': {'n': 1}}, reply.payload
print('RIDE_E2E_PING_OK', flush=True)
"""

# scenario B root: ping (with a forged identity claim in the payload), then spawn a
# child and record every message as received, wire-level, into a report file
_PROBE_B_ROOT = """
import json, os, sys, time, traceback
from pathlib import Path

report = {'messages': []}

def main():
  from bro.broker.brotocol import Message
  from bro.broker.transport import connect

  deadline = float(os.environ['RIDE_E2E_DEADLINE'])
  exit_after = os.environ['RIDE_E2E_EXIT_AFTER']
  transport = connect(os.environ['BROKER_CHANNEL'])
  ping = Message(type='ping', payload={'n': 1, 'from': 'forged-peer-identity'})
  transport.send(ping)
  report['ping_id'] = ping.id
  reply = transport.receive(30)
  assert reply is not None, 'no ping reply'
  report['ping_reply'] = {
    'type': reply.type,
    'in_reply_to': reply.in_reply_to,
    'payload': reply.payload,
  }
  request = Message(type='spawn', payload={})
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
      'in_reply_to': message.in_reply_to,
      'payload': message.payload,
      'elapsed': time.monotonic() - start,
    })
    if message.type in ('completed', 'failed'):
      break
    if exit_after == 'started' and message.type == 'started':
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
assert 'message not sent' in inert.stderr, inert.stderr
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
# workspace's own code; under RIDE_E2E_LINGER it traps SIGTERM (exit 7) so the
# harness can assert `docker stop` reaches claude through tini → runner.
_INPLACE_WRAPPER = """
mkdir -p /tmp/e2e-bin
cat > /tmp/e2e-bin/claude <<'FAKE'
#!/usr/bin/env python3
import json, os, signal, sys, time
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
  signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(7))
  Path('/workspace/.e2e-ready').touch()
  time.sleep(90)
  sys.exit(5)
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

launch = Launch(name=os.environ['RIDE_E2E_NAME'],
                command=json.loads(os.environ['RIDE_E2E_COMMAND']), env={},
                secrets=tuple(json.loads(os.environ.get('RIDE_E2E_SECRETS', '[]'))),
                docker_sock=True, tty=True, forward_env=True)
workspace = Workspace.ensure(launch.name, project_root(), WorkspaceKind.CONTAINER)
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

  @property
  def broker_dir(self) -> Path:
    return self.runtime_root / 'broker'

  @property
  def workspaces_dir(self) -> Path:
    return self.runtime_root / 'workspaces'

  def tree(self, name: str) -> Path:
    return self.workspaces_dir / name / 'tree'

  def sockets(self) -> list[Path]:
    if not self.broker_dir.is_dir():
      return []
    return sorted(self.broker_dir.glob('*.sock'))

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
  # short prefix directly under the system temp dir: the socket path must fit sun_path
  root = Path(tempfile.mkdtemp(prefix=_NAME_PREFIX))
  checkout = Path(
    subprocess.run(
      ['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', '--show-toplevel'],
      capture_output=True,
      text=True,
      check=True,
    ).stdout.strip()
  )
  project = root / 'project'
  subprocess.run(['git', 'clone', '--quiet', str(checkout), str(project)], check=True)
  data_home = root / 'state'
  home = root / 'home'
  home.mkdir()
  # scenarios that exec the real runner hydrate `brog` (RIDE_E2E_SECRETS): the
  # session MCP server builds brog's backend from that secret at assembly, so
  # the health gate needs the stub a real session's scoped store would carry.
  # construction is offline — nothing contacts GitHub
  bro_dir = home / '.bro'
  bro_dir.mkdir()
  (bro_dir / 'brog.json').write_text(
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
  # the clone's pyproject/uv.lock are identical to the checkout's, so this resolves to
  # the image real sessions already built; builds only on a host that never launched one
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setattr(workspace_docker, 'project_root', lambda: project)
    workspace_docker._ensure_image(workspace_docker.image_tag())
    monkeypatch.setenv('XDG_DATA_HOME', str(data_home))
    runtime_root = workspace_paths.runtime_root(project)
  env = IsolatedEnv(
    root=root,
    project=project,
    home=home,
    data_home=data_home,
    runtime_root=runtime_root,
  )
  yield env
  _remove_stray_containers(env)
  shutil.rmtree(root, ignore_errors=True)


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
  sockets_during: list[Path] = field(default_factory=list)
  socket_mode: Optional[int] = None
  broker_dir_mode: Optional[int] = None
  container_id: Optional[str] = None
  socket_mounted_at: Optional[str] = None
  max_sockets: int = 0
  sockets_after: list[Path] = field(default_factory=list)
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


def _container_mount_of(container_id: str, source: Path) -> Optional[str]:
  """the in-container destination `source` is bind-mounted at, or None."""
  inspect = subprocess.run(
    ['docker', 'inspect', '--format', '{{json .Mounts}}', container_id],
    capture_output=True,
    text=True,
  )
  if inspect.returncode != 0:
    return None
  for mount in json.loads(inspect.stdout):
    if mount.get('Source') == str(source):
      return mount.get('Destination')
  return None


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
  driver = _Driver(env, name, ['python', '-c', _PROBE_A], extra_env={})
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  sockets = env.sockets()
  run = LiveRun(exit_code=-1, output='', sockets_during=sockets)
  if len(sockets) == 1:
    run.socket_mode = stat.S_IMODE(sockets[0].stat().st_mode)
    run.broker_dir_mode = stat.S_IMODE(env.broker_dir.stat().st_mode)
    run.container_id = find_container_id(env.tree(name))
    if run.container_id is not None:
      run.socket_mounted_at = _container_mount_of(run.container_id, sockets[0])
  (env.tree(name) / '.e2e-continue').touch()
  run.exit_code = driver.wait(120)
  run.output = driver.output()
  run.sockets_after = env.sockets()
  run.container_gone_after = _container_gone(env, name, 15)
  return run


class TestBrokerEnabledLaunch:
  def test_channel_provisioned_and_bind_mounted(self, scenario_a: LiveRun) -> None:
    assert len(scenario_a.sockets_during) == 1, scenario_a.sockets_during
    assert scenario_a.broker_dir_mode == 0o700
    assert scenario_a.socket_mode == 0o600
    assert scenario_a.container_id is not None
    assert scenario_a.socket_mounted_at == '/run/broker.sock'

  def test_teardown_after_root_exit(self, scenario_a: LiveRun) -> None:
    assert scenario_a.sockets_after == [], 'channel socket not unlinked after the root exited'
    assert scenario_a.container_gone_after, 'session container survived the root exit'

  def test_ping_round_trip_over_live_channel(self, scenario_a: LiveRun) -> None:
    assert 'RIDE_E2E_PING_OK' in scenario_a.output and scenario_a.exit_code == 0, (
      f'a ping over the live launch path got no correlated reply — the launch-path broker '
      f'refuses typed requests (driver exit {scenario_a.exit_code})\n{scenario_a.output}'
    )


# --- B: child lifecycle over the real ports ----------------------------------


@dataclass
class BrokerRun:
  """observations from one in-process Broker run over the real transport + spawner."""

  code: int
  report: dict
  root_peer: Optional[Peer]
  observed_pings: list[tuple[Peer, dict]]
  max_sockets: int
  max_live: int
  sockets_after: list[Path]
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
      command=['python', '-c', _PROBE_B_ROOT],
      env={'RIDE_E2E_DEADLINE': str(probe_deadline), 'RIDE_E2E_EXIT_AFTER': exit_after},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
    )
  )
  child = DockerLaunchSpec(
    workspace_docker.Launch(
      name=f'{name}-child',
      command=child_command,
      env={},
      secrets=(),
      docker_sock=False,
      tty=False,
      forward_env=False,
    )
  )
  facade = Broker(
    UnixServerTransport(str(env.broker_dir)), DockerSpawner(), default_timeout=default_timeout
  )
  observed_pings: list[tuple[Peer, dict]] = []

  def recording_ping(context: Dispatcher, peer: Peer, message: Message) -> None:
    observed_pings.append((peer, dict(message.payload)))
    ping_handler(context, peer, message)

  facade.on('ping', recording_ping)
  facade.on('spawn', spawn_test_handler(child))

  result: dict[str, int] = {}
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setenv('HOME', str(env.home))
    monkeypatch.setattr(ride.spawn, 'project_root', lambda: env.project)
    monkeypatch.setattr(workspace_spawn, 'project_root', lambda: env.project)
    monkeypatch.setattr(workspace_docker, 'project_root', lambda: env.project)
    thread = threading.Thread(target=lambda: result.update(code=facade.run(root)))
    thread.start()
    max_sockets = 0
    max_live = 0
    deadline = time.monotonic() + budget
    while thread.is_alive() and time.monotonic() < deadline:
      max_sockets = max(max_sockets, len(env.sockets()))
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
    max_sockets=max_sockets,
    max_live=max_live,
    sockets_after=env.sockets(),
    live_after=env.live_containers(),
    workspace_leaks=env.leaked_dirs(env.workspaces_dir),
  )


@pytest.fixture(scope='module')
def b_clean(isolated_env: IsolatedEnv) -> BrokerRun:
  return _run_broker_scenario(
    isolated_env,
    'clean',
    ['python', '-c', _CHILD_CLEAN],
    default_timeout=600,
    probe_deadline=120,
  )


@pytest.fixture(scope='module')
def b_early_exit(isolated_env: IsolatedEnv) -> BrokerRun:
  return _run_broker_scenario(
    isolated_env,
    'early',
    ['python', '-c', _CHILD_EARLY_EXIT],
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
    ['python', '-c', _CHILD_STARTED_THEN_HANG],
    default_timeout=600,
    probe_deadline=120,
    exit_after='started',
  )


class TestChildLifecycle:
  def test_ping_reply_and_channel_pinned_identity(self, b_clean: BrokerRun) -> None:
    assert 'error' not in b_clean.report, b_clean.report.get('error')
    reply = b_clean.report['ping_reply']
    assert reply['type'] == 'reply'
    assert reply['in_reply_to'] == b_clean.report['ping_id']
    assert reply['payload'] == {'pong': {'n': 1, 'from': 'forged-peer-identity'}}
    # the dispatcher attributed the request to the socket's own channel, not the
    # forged payload claim — identity is pinned to the channel the message arrived on
    assert b_clean.root_peer is not None
    assert b_clean.observed_pings == [(b_clean.root_peer, {'n': 1, 'from': 'forged-peer-identity'})]

  def test_child_lifecycle_routed_to_parent(self, b_clean: BrokerRun) -> None:
    assert b_clean.code == 0
    request_id = b_clean.report['request_id']
    types = [m['type'] for m in b_clean.report['messages']]
    assert types == ['started', 'completed'], b_clean.report['messages']
    started, completed = b_clean.report['messages']
    assert started['in_reply_to'] == request_id
    assert started['payload'] == {'trail_id': 'e2e-trail'}
    assert completed['in_reply_to'] == request_id
    assert completed['payload'] == {'result': 'child-ok', 'end_reason': 'ok'}
    assert b_clean.max_sockets == 2
    assert b_clean.max_live == 2
    assert b_clean.sockets_after == []
    assert b_clean.live_after == []

  def test_no_workspace_dirs_leaked_after_parent_exit(self, b_clean: BrokerRun) -> None:
    assert b_clean.workspace_leaks == [], (
      'spawned child left workspace state behind after the parent exited: '
      f'{b_clean.workspace_leaks}'
    )

  def test_early_exit_child_synthesizes_failed(self, b_early_exit: BrokerRun) -> None:
    assert b_early_exit.code == 0
    types = [m['type'] for m in b_early_exit.report['messages']]
    assert types == ['failed'], b_early_exit.report['messages']
    failed = b_early_exit.report['messages'][0]
    assert failed['in_reply_to'] == b_early_exit.report['request_id']
    assert failed['payload']['reason'] == 'exit'
    assert failed['payload']['exit_code'] == 3
    # stdout and stderr are merged into the one output tail
    assert 'e2e-stdout-marker' in failed['payload']['output_tail']
    assert 'e2e-stderr-marker' in failed['payload']['output_tail']
    assert b_early_exit.sockets_after == []
    assert b_early_exit.live_after == []

  def test_wedged_child_times_out_at_default_timeout(self, b_timeout: BrokerRun) -> None:
    assert b_timeout.code == 0
    types = [m['type'] for m in b_timeout.report['messages']]
    assert types == ['failed'], b_timeout.report['messages']
    failed = b_timeout.report['messages'][0]
    assert failed['in_reply_to'] == b_timeout.report['request_id']
    assert failed['payload'] == {'reason': 'timeout'}
    # the timer starts once the child is spawned, strictly after the request went out,
    # and fires at exactly default_timeout; the slack above covers the spawn overhead
    assert 30 <= failed['elapsed'] <= 60, failed['elapsed']
    assert b_timeout.sockets_after == []
    assert b_timeout.live_after == [], 'timed-out child container not killed'

  def test_children_torn_down_on_root_exit(self, b_teardown: BrokerRun) -> None:
    assert b_teardown.code == 0
    types = [m['type'] for m in b_teardown.report['messages']]
    assert types == ['started'], b_teardown.report['messages']
    assert b_teardown.sockets_after == []
    assert b_teardown.live_after == [], 'live child container survived the root exit'


# --- C: BROKER_DISABLED kill-switch -------------------------------------------


@pytest.fixture(scope='module')
def scenario_c(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}c-root'
  driver = _Driver(
    env, name, ['python', '-c', _PROBE_NO_CHANNEL], extra_env={'BROKER_DISABLED': '1'}
  )
  request.addfinalizer(driver.close)
  run = LiveRun(exit_code=-1, output='')
  deadline = time.monotonic() + 240
  while driver.process.poll() is None and time.monotonic() < deadline:
    run.max_sockets = max(run.max_sockets, len(env.sockets()))
    time.sleep(0.25)
  run.exit_code = driver.wait(30)
  run.output = driver.output()
  run.sockets_after = env.sockets()
  return run


class TestKillSwitch:
  def test_session_launches_cleanly_without_broker(self, scenario_c: LiveRun) -> None:
    assert scenario_c.exit_code == 0, scenario_c.output
    assert 'RIDE_E2E_NO_CHANNEL_OK' in scenario_c.output

  def test_short_circuits_before_any_broker_import(self, scenario_c: LiveRun) -> None:
    assert scenario_c.broker_modules == []

  def test_no_socket_provisioned(self, scenario_c: LiveRun) -> None:
    assert scenario_c.max_sockets == 0
    assert scenario_c.sockets_after == []


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
    env, name, ['python', '-c', _PROBE_NO_CHANNEL], extra_env={'PYTHONPATH': str(shadow)}
  )
  request.addfinalizer(driver.close)
  run = LiveRun(exit_code=-1, output='')
  deadline = time.monotonic() + 240
  while driver.process.poll() is None and time.monotonic() < deadline:
    run.max_sockets = max(run.max_sockets, len(env.sockets()))
    time.sleep(0.25)
  run.exit_code = driver.wait(30)
  run.output = driver.output()
  run.sockets_after = env.sockets()
  return run


class TestBrokerUnimportable:
  def test_degrades_to_direct_launch_with_warning(self, scenario_d: LiveRun) -> None:
    assert scenario_d.exit_code == 0, scenario_d.output
    assert 'broker package not importable' in scenario_d.output
    assert 'RIDE_E2E_NO_CHANNEL_OK' in scenario_d.output
    assert scenario_d.broker_modules == []

  def test_no_socket_provisioned(self, scenario_d: LiveRun) -> None:
    assert scenario_d.max_sockets == 0
    assert scenario_d.sockets_after == []


# --- E: SIGINT through the attached root --------------------------------------


@pytest.fixture(scope='module')
def scenario_e_ctrl_c(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}e-ctrlc-root'
  driver = _Driver(env, name, ['python', '-c', _PROBE_SIGINT], extra_env={})
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  driver.write(b'\x03')  # ^C on the session terminal, forwarded raw into the container tty
  run = LiveRun(exit_code=driver.wait(60), output=driver.output())
  run.sockets_after = env.sockets()
  run.container_gone_after = _container_gone(env, name, 15)
  return run


@pytest.fixture(scope='module')
def scenario_e_targeted(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}e-targeted-root'
  driver = _Driver(env, name, ['python', '-c', _PROBE_SIGINT], extra_env={})
  request.addfinalizer(driver.close)
  _wait_ready(env, name, driver)
  container_id = find_container_id(env.tree(name))
  os.kill(driver.process.pid, signal.SIGINT)  # targeted at the launcher, not the terminal group
  run = LiveRun(exit_code=driver.wait(60), output=driver.output(), container_id=container_id)
  run.sockets_after = env.sockets()
  run.container_gone_after = _container_gone(env, name, 15)
  if not run.container_gone_after and container_id is not None:
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)
  return run


class TestSigintHandling:
  def test_terminal_ctrl_c_clean_teardown(self, scenario_e_ctrl_c: LiveRun) -> None:
    assert 'RIDE_E2E_SIGINT_CAUGHT' in scenario_e_ctrl_c.output
    assert scenario_e_ctrl_c.exit_code == 0, scenario_e_ctrl_c.output
    assert 'Traceback' not in scenario_e_ctrl_c.output
    assert scenario_e_ctrl_c.sockets_after == []
    assert scenario_e_ctrl_c.container_gone_after

  def test_targeted_sigint_does_not_unwind_the_loop(self, scenario_e_targeted: LiveRun) -> None:
    assert 'KeyboardInterrupt' not in scenario_e_targeted.output, scenario_e_targeted.output
    assert 'Traceback' not in scenario_e_targeted.output, scenario_e_targeted.output
    assert scenario_e_targeted.sockets_after == [], 'teardown did not unlink the channel socket'

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
    _inplace_command('ride', 'ss', '--in-place', '--fast', name),
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
    assert 'at' in mcp_config['mcpServers']
    assert report['session_context_set'] is True

  def test_fast_mode_reaches_the_merged_settings(
    self, scenario_f: LiveRun, isolated_env: IsolatedEnv
  ) -> None:
    report = _report(isolated_env, f'{_NAME_PREFIX}f-root')
    assert report['settings']['fastMode'] is True


# --- G: SIGTERM forwarding — `docker stop` lands in claude ---------------------


@pytest.fixture(scope='module')
def scenario_g(isolated_env: IsolatedEnv, request: pytest.FixtureRequest) -> LiveRun:
  env = isolated_env
  name = f'{_NAME_PREFIX}g-root'
  driver = _Driver(
    env,
    name,
    _inplace_command('env', 'RIDE_E2E_LINGER=1', 'ride', 'ss', '--in-place', name),
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
  def test_sigterm_forwarded_and_exit_code_propagated(self, scenario_g: LiveRun) -> None:
    # docker stop SIGTERMs pid 1 (tini), which forwards to the exec'd runner,
    # which forwards to claude — whose TERM handler exits 7. anything else
    # (SIGKILL after the grace period) would surface as 137/143.
    assert scenario_g.reported_exit == '7', scenario_g.output
    assert scenario_g.exit_code == 7

  def test_container_torn_down(self, scenario_g: LiveRun) -> None:
    assert scenario_g.container_gone_after
