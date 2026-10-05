"""Live broker, worker-container, Chromium, artifact, and VNC routes for webview."""

import contextlib
import errno
import http.server
import json
import os
import pty
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import ride.clean as ride_clean
import ride.workspace.docker as workspace_docker
import ride.workspace.host_docker_test_helper as host_docker
from bro.base import credentials
from bro.broker.transports.tcp import LOCAL_HOST
from bro.webview.profile import PROFILE_VERSION
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
from ride.artifacts import view_mount
from ride.e2e_test import (
  _RUNTIME_PYTHON,
  IsolatedEnv,
  _session_broxy_probe,
  _wait_until,
  isolated_env as isolated_env,
)
from ride.runtime_bundle import RuntimeBundle
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver, find_container_id
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import DockerLaunchSpec

pytestmark = host_docker.HOST_DAEMON_ONLY

_DENIED_ROUTE = r"""
import subprocess
from pathlib import Path

completed = subprocess.run(['webview', 'open'], capture_output=True, text=True)
assert completed.returncode == 1, completed
assert ':launch.webview' in completed.stderr, completed.stderr
Path('/workspace/.webview-denied-report').write_text('denied')
"""


_SUMMON_WEBVIEW_ROUTE = r"""
from pathlib import Path
from bro.summon import summon_and_wait

answer = summon_and_wait(
  'bro',
  'open the granted webview',
  grant=[':launch.webview'],
  llm='echo',
  harness='bro',
  timeout=300,
)
Path('/workspace/.webview-summon-report').write_text(answer)
"""


_SUMMON_WEBVIEW_CHILD = r"""
import json
import subprocess
from bro.run_lifecycle import RunLifecycle

opened = subprocess.run(['webview', 'open'], capture_output=True, text=True)
assert opened.returncode == 0, opened.stderr
mission = json.loads(opened.stdout)['mission']
closed = subprocess.run(['webview', 'close', mission], capture_output=True, text=True)
assert closed.returncode == 0, closed.stderr
channel = RunLifecycle.from_env()
assert channel is not None
channel.trail('summoned-webview-child')
channel.completed('opened', 'ok')
channel.close()
"""


_NESTED_WEBVIEW_ROUTE = r"""
import json
import subprocess
from pathlib import Path

opened = subprocess.run(['webview', 'open'], capture_output=True, text=True)
assert opened.returncode == 0, opened.stderr
Path('/workspace/.webview-nested-report').write_text(json.loads(opened.stdout)['mission'])
"""


_NESTED_WEBVIEW_WORKER = r"""
import os
import traceback
from bro.broker.client import Client
from bro.broker.environment import BROKER_MISSION

mission = os.environ[BROKER_MISSION]
client = Client.from_env()
assert client is not None
with client:
  client.listen(mission)
  try:
    nested = client.call('launch', {'type': 'webview'}, 30)
    assert nested.payload.get('outcome') == 'denied', nested.payload
    assert ':launch.webview' in nested.payload.get('error', ''), nested.payload
  except Exception:
    client.result(mission, {'outcome': 'failed', 'error': traceback.format_exc()})
    raise
  client.message(mission, {'event': 'ready', 'vnc': None})
  client.result(mission, {'outcome': 'ok', 'value': {'commands': 0}})
"""


_FULL_ROUTE = r"""
import json
import re
import subprocess
import time
import traceback
import urllib.parse
from pathlib import Path

workspace = Path('/workspace')
report_path = workspace / '.webview-e2e-report.json'
error_path = workspace / '.webview-e2e-error'


def run(*arguments, expected=0):
  completed = subprocess.run(arguments, capture_output=True, text=True)
  if completed.returncode != expected:
    raise RuntimeError(
      f'{arguments!r} returned {completed.returncode}, expected {expected}'
      f'\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}'
    )
  return completed


def ask(mission, payload):
  completed = run(
    'mission',
    'ask',
    mission,
    json.dumps(payload, separators=(',', ':')),
    '--wait',
    '180',
  )
  return json.loads(completed.stdout)


def one_file(reply):
  files = reply.get('files')
  assert isinstance(files, list) and len(files) == 1, reply
  file = files[0]
  assert set(file) == {'name', 'ref'}, file
  path = Path('/var/ride/artifacts') / file['ref']
  assert path.is_file(), path
  return file, path.read_bytes()


try:
  denied = run('webview', 'open', '--vnc', expected=1)
  assert ':launch.webview.vnc' in denied.stderr, denied.stderr

  upload = workspace / 'upload.txt'
  upload.write_text('shared upload payload')
  from bro.artifact import mint_artifact
  upload_ref = mint_artifact('upload.txt', timeout=30).ref

  opened = json.loads(run('webview', 'open', '--share', upload_ref).stdout)
  mission = opened['mission']
  assert opened['vnc'] is None, opened

  html = '''<!doctype html><html><body>
  <h1>webview end to end</h1>
  <input id="upload" type="file">
  <a id="download" download="offered.txt" href="data:text/plain,offered-download">download</a>
  </body></html>'''
  data_url = 'data:text/html,' + urllib.parse.quote(html)
  navigated = ask(mission, {'tool': 'browser_navigate', 'arguments': {'url': data_url}})
  assert 'error' not in navigated, navigated
  snapshot = ask(mission, {'tool': 'browser_snapshot', 'arguments': {}})
  assert 'webview end to end' in snapshot.get('text', ''), snapshot

  history = json.loads(run('mission', 'history', mission).stdout)
  messages = history['messages']
  assert any(entry.get('head', {}).get('tool') == 'browser_navigate' for entry in messages)
  assert any(
    entry.get('reply_to') is not None and 'webview end to end' in entry.get('head', {}).get('text', '')
    for entry in messages
  )

  automatic = ask(
    mission,
    {'tool': 'browser_take_screenshot', 'arguments': {'scale': 'css'}},
  )
  automatic_file, automatic_bytes = one_file(automatic)
  assert automatic_bytes.startswith(b'\x89PNG\r\n\x1a\n')
  assert automatic_file['name'].startswith('output/page-'), automatic_file

  named = ask(
    mission,
    {
      'tool': 'browser_take_screenshot',
      'arguments': {'filename': 'named.png', 'scale': 'css'},
    },
  )
  named_file, named_bytes = one_file(named)
  assert named_file['name'] == 'named.png', named_file
  assert named_bytes.startswith(b'\x89PNG\r\n\x1a\n')

  (workspace / '.webview-files-ready').write_text(
    json.dumps({'mission': mission, 'files': [automatic_file, named_file]})
  )
  deadline = time.monotonic() + 60
  while not (workspace / '.webview-e2e-continue').exists():
    if time.monotonic() >= deadline:
      raise TimeoutError('host did not inspect the live webview workspace')
    time.sleep(0.2)

  clicked_upload = ask(
    mission,
    {'tool': 'browser_click', 'arguments': {'element': 'file input', 'target': '#upload'}},
  )
  assert 'error' not in clicked_upload, clicked_upload
  uploaded = ask(
    mission,
    {
      'tool': 'browser_file_upload',
      'arguments': {'paths': [f'/workspace/artifacts/{upload_ref}']},
    },
  )
  assert 'error' not in uploaded, uploaded
  uploaded_text = ask(
    mission,
    {
      'tool': 'browser_evaluate',
      'arguments': {
        'function': "async () => await document.querySelector('#upload').files[0].text()"
      },
    },
  )
  assert 'shared upload payload' in uploaded_text.get('text', ''), uploaded_text

  unsafe = ask(
    mission,
    {'tool': 'browser_run_code_unsafe', 'arguments': {'code': 'async () => 1'}},
  )
  assert 'withholds' in unsafe.get('error', ''), unsafe
  local_file = ask(
    mission,
    {'tool': 'browser_navigate', 'arguments': {'url': 'file:///etc/passwd'}},
  )
  assert 'error' in local_file, local_file

  run('mission', 'ask', mission, json.dumps({'tool': 'browser_navigate', 'arguments': {'url': data_url}}), '--wait', '180')
  download = ask(
    mission,
    {'tool': 'browser_click', 'arguments': {'element': 'download link', 'target': '#download'}},
  )
  assert download.get('files', []) == [], download
  assert 'error' in download or 'download' in download.get('text', '').lower(), download
  after_download = ask(mission, {'tool': 'browser_snapshot', 'arguments': {}})
  assert 'webview end to end' in after_download.get('text', ''), after_download
  assert after_download.get('files') == [], after_download
  (workspace / '.webview-download-ready').touch()
  deadline = time.monotonic() + 60
  while not (workspace / '.webview-download-continue').exists():
    if time.monotonic() >= deadline:
      raise TimeoutError('host did not inspect the refused download')
    time.sleep(0.2)

  page_url = __PAGE_URL__
  remote = ask(
    mission,
    {'tool': 'browser_navigate', 'arguments': {'url': page_url}},
  )
  assert 'error' not in remote, remote
  requests = ask(
    mission,
    {
      'tool': 'browser_network_requests',
      'arguments': {'static': True, 'filter': re.escape(page_url)},
    },
  )
  indexes = [int(value) for value in re.findall(r'(?m)^(\d+)\.', requests.get('text', ''))]
  assert len(indexes) == 1, requests
  response_body = ask(
    mission,
    {
      'tool': 'browser_network_request',
      'arguments': {
        'index': indexes[0],
        'part': 'response-body',
        'filename': 'network-body.html',
      },
    },
  )
  body_file, body = one_file(response_body)
  assert body_file['name'] == 'network-body.html', body_file
  assert body == __NETWORK_PAGE__, body[:200]

  closed = json.loads(run('webview', 'close', mission).stdout)
  assert closed['outcome'] == 'ok', closed
  report_path.write_text(
    json.dumps(
      {
        'mission': mission,
        'upload_ref': upload_ref,
        'artifact_refs': [
          automatic_file['ref'],
          named_file['ref'],
          body_file['ref'],
        ],
        'closed': closed,
      }
    )
  )
except Exception:
  error_path.write_text(traceback.format_exc())
  raise
"""

_PROFILE_ROUTE = r"""
import json
import re
import subprocess
import traceback
from pathlib import Path

from bro.base import credentials

workspace = Path('/workspace')
report_path = workspace / '.webview-profile-report.json'
error_path = workspace / '.webview-profile-error'


def run(*arguments, expected=0):
  completed = subprocess.run(arguments, capture_output=True, text=True)
  if completed.returncode != expected:
    raise RuntimeError(
      f'{arguments!r} returned {completed.returncode}, expected {expected}'
      f'\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}'
    )
  return completed


def ask(mission, payload):
  completed = run(
    'mission',
    'ask',
    mission,
    json.dumps(payload, separators=(',', ':')),
    '--wait',
    '180',
  )
  return json.loads(completed.stdout)


try:
  assert credentials.try_get('cookies') is None
  denied = run('webview', 'open', '--cookies', 'denied', expected=1)
  assert "beyond the owner's" in denied.stderr, denied.stderr

  opened = json.loads(run('webview', 'open', '--cookies', 'e2e').stdout)
  mission = opened['mission']
  navigated = ask(
    mission,
    {'tool': 'browser_navigate', 'arguments': {'url': 'https://example.com/'}},
  )
  assert 'error' not in navigated, navigated
  browser_state = ask(
    mission,
    {
      'tool': 'browser_evaluate',
      'arguments': {
        'function': "() => ({cookies: document.cookie, local: localStorage.getItem('profile-state')})"
      },
    },
  )
  state_text = browser_state.get('text', '')
  assert 'visible-profile-cookie' in state_text, browser_state
  assert 'profile-local-storage' in state_text, browser_state
  assert 'hidden-profile-cookie' not in state_text, browser_state

  requests = ask(
    mission,
    {
      'tool': 'browser_network_requests',
      'arguments': {'static': True, 'filter': 'example\\.com'},
    },
  )
  indexes = [int(value) for value in re.findall(r'(?m)^(\d+)\.', requests.get('text', ''))]
  assert indexes, requests
  request = ask(
    mission,
    {'tool': 'browser_network_request', 'arguments': {'index': indexes[-1]}},
  )
  assert 'hidden-profile-cookie' not in request.get('text', ''), request

  closed = json.loads(run('webview', 'close', mission).stdout)
  assert closed['outcome'] == 'ok', closed
  report_path.write_text(json.dumps({'browser_state': browser_state, 'request': request}))
except Exception:
  error_path.write_text(traceback.format_exc())
  raise
"""


_VNC_ROUTE = r"""
import json
import subprocess
import time
import traceback
from pathlib import Path

workspace = Path('/workspace')
report_path = workspace / '.webview-vnc-report.json'
error_path = workspace / '.webview-vnc-error'

try:
  opened_process = subprocess.run(
    ['webview', 'open', '--vnc', '--port', '__VNC_PORT__'],
    capture_output=True,
    text=True,
    check=True
  )
  opened = json.loads(opened_process.stdout)
  assert isinstance(opened['vnc'], str), opened
  report_path.write_text(json.dumps(opened))
  deadline = time.monotonic() + 60
  while not (workspace / '.webview-vnc-continue').exists():
    if time.monotonic() >= deadline:
      raise TimeoutError('host did not probe the published noVNC port')
    time.sleep(0.2)
  closed_process = subprocess.run(
    ['webview', 'close', opened['mission']], capture_output=True, text=True, check=True
  )
  closed = json.loads(closed_process.stdout)
  assert closed['outcome'] == 'ok', closed
except Exception:
  error_path.write_text(traceback.format_exc())
  raise
"""

_KILLED_ROUTE = r"""
import json
import subprocess
import traceback
from pathlib import Path

workspace = Path('/workspace')
report_path = workspace / '.webview-killed-report.json'
error_path = workspace / '.webview-killed-error'

try:
  opened_process = subprocess.run(
    ['webview', 'open'], capture_output=True, text=True, check=True
  )
  opened = json.loads(opened_process.stdout)
  subprocess.run(
    ['mission', 'cancel', opened['mission'], '--timeout', '180'],
    capture_output=True,
    text=True,
    check=True,
  )
  report_path.write_text(json.dumps(opened))
except Exception:
  error_path.write_text(traceback.format_exc())
  raise
"""


@contextlib.contextmanager
def _route_observer(
  workspace: Workspace,
  observer: Callable[[Workspace, threading.Event], None],
) -> Iterator[None]:
  observer_errors: list[BaseException] = []
  route_ended = threading.Event()

  def observe() -> None:
    try:
      observer(workspace, route_ended)
    except BaseException as error:
      observer_errors.append(error)

  observer_thread = threading.Thread(target=observe, daemon=True)
  observer_thread.start()

  def finish() -> None:
    route_ended.set()
    observer_thread.join(10)

  try:
    yield
  except BaseException as error:
    finish()
    if observer_thread.is_alive():
      error.add_note('route observer did not stop within 10 seconds')
    for observer_error in observer_errors:
      error.add_note(f'route observer also failed: {observer_error!r}')
    raise
  else:
    finish()
    assert not observer_thread.is_alive()
    if observer_errors:
      raise observer_errors[0]


def _run_route(
  env: IsolatedEnv,
  monkeypatch: pytest.MonkeyPatch,
  *,
  suffix: str,
  source: str,
  vnc: bool,
  observer: Callable[[Workspace, threading.Event], None],
  allowed: bool = True,
  launch_scope: dict | None = None,
) -> tuple[int, Workspace]:
  import ride.broker_root as broker_root

  name = f'ride-e2e-webview-{suffix}'
  workspace = Workspace.ensure(name, env.project, Isolation.BOXED)
  runtime_bundle = RuntimeBundle(
    env.runtime_root / 'runtime' / env.runtime_bundle_hash,
    f'{sys.version_info.major}.{sys.version_info.minor}',
  )
  container_runtime = ContainerRuntimeResolver.fixed(
    ContainerRuntime(env.image, env.runtime_bundle_hash, env.runtime_image),
    workspace.repository,
  )
  monkeypatch.setenv('HOME', str(env.home))
  launch = DockerLaunchSpec(
    workspace_docker.Launch(
      name=name,
      command=_session_broxy_probe(source),
      env={'RIDE_BRO': 'bro-dev'},
      secrets=(),
      tty=False,
      image=env.image,
      runtime_bundle_hash=env.runtime_bundle_hash,
      extra_mounts=(
        view_mount(
          workspace.name,
          workspace.name,
          PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
        ),
      ),
      repo=env.project,
    )
  )
  with _route_observer(workspace, observer):
    code = broker_root.run_root_via_broker(
      launch,
      workspace=workspace,
      bro='bro-dev',
      launch_scope=(
        launch_scope
        if launch_scope is not None
        else ({'webview': {'vnc': True} if vnc else {}} if allowed else {})
      ),
      container_runtime=container_runtime,
      runtime_bundle=runtime_bundle,
    )
  return code, workspace


def _diagnostic(workspace: Workspace, error_name: str) -> str:
  path = workspace.tree / error_name
  return path.read_text() if path.is_file() else 'the root wrote no error report'


def _available_port() -> int:
  with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    return listener.getsockname()[1]


# no subresources, and a data: icon so the browser requests no favicon either
_NETWORK_PAGE = b'<!doctype html><link rel=icon href=data:,><title>webview network body</title>'


@contextlib.contextmanager
def _served_page(page: bytes) -> Iterator[str]:
  """serve `page` on the one address a container reaches the host at, yielding the
  URL a container fetches it at."""
  import ride.broker_root as broker_root

  class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
      self.send_response(200)
      self.send_header('Content-Type', 'text/html')
      self.send_header('Content-Length', str(len(page)))
      self.end_headers()
      self.wfile.write(page)

  gateways = [host for host in broker_root.broker_bind_hosts() if host != LOCAL_HOST]
  host = gateways[0] if len(gateways) > 0 else LOCAL_HOST
  with contextlib.ExitStack() as stack:
    server = stack.enter_context(http.server.ThreadingHTTPServer((host, 0), Handler))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    yield f'http://{workspace_docker.CONTAINER_BROKER_HOST}:{server.server_address[1]}/'


@contextlib.contextmanager
def _setup_terminal_process(
  arguments: list[str], environment: dict[str, str]
) -> Iterator[tuple[subprocess.Popen, int]]:
  master, slave = pty.openpty()
  process = None
  try:
    process = subprocess.Popen(
      arguments,
      stdin=slave,
      stdout=slave,
      stderr=slave,
      env=environment,
    )
    os.close(slave)
    slave = -1
    yield process, master
  finally:
    if process is not None and process.poll() is None:
      process.kill()
      process.wait()
    if slave != -1:
      os.close(slave)
    os.close(master)


def _read_terminal(master: int) -> bytes:
  try:
    return os.read(master, 65536)
  except OSError as error:
    if error.errno == errno.EIO:
      return b''
    raise


def _run_setup(store: Path, port: int) -> str:
  environment = dict(os.environ)
  environment['BRO_STORE'] = str(store)
  environment.pop('RIDE_ISOLATION', None)
  output = bytearray()
  with _setup_terminal_process(
    [
      'webview',
      'setup',
      'e2e',
      '--url',
      'https://example.com/',
      '--indexed-db',
      '--port',
      str(port),
    ],
    environment,
  ) as (process, master):
    ready_deadline = time.monotonic() + 1500
    while b'press Enter to capture' not in output:
      if process.poll() is not None:
        break
      if time.monotonic() >= ready_deadline:
        raise TimeoutError('webview setup did not publish its login prompt')
      readable, _, _ = select.select([master], [], [], 0.2)
      if readable:
        output.extend(_read_terminal(master))
    if b'press Enter to capture' not in output:
      raise RuntimeError(
        f'webview setup exited before its prompt:\n{output.decode(errors="replace")}'
      )
    with urllib.request.urlopen(
      f'http://127.0.0.1:{port}/vnc.html?autoconnect=1&resize=scale', timeout=15
    ) as response:
      assert response.status == 200
      assert b'noVNC' in response.read()
    os.write(master, b'\n')
    finish_deadline = time.monotonic() + 300
    while process.poll() is None:
      if time.monotonic() >= finish_deadline:
        raise TimeoutError('webview setup did not finish its capture')
      readable, _, _ = select.select([master], [], [], 0.2)
      if readable:
        output.extend(_read_terminal(master))
    while True:
      readable, _, _ = select.select([master], [], [], 0)
      if not readable:
        break
      chunk = _read_terminal(master)
      if not chunk:
        break
      output.extend(chunk)
    return_code = process.returncode
  rendered = output.decode(errors='replace')
  assert return_code == 0, rendered
  return rendered


def _live_containers(workspace: Workspace) -> list[str]:
  return [
    directory.name
    for directory in workspace.path.parent.iterdir()
    if find_container_id(directory / 'tree') is not None
  ]


def test_webview_setup_captures_seeded_cookie_and_indexed_db_on_requested_port(
  isolated_env: IsolatedEnv,
) -> None:
  store = isolated_env.home / '.bro-setup'
  material = store / 'creds' / 'cookies+e2e.cred'
  material.parent.mkdir(parents=True)
  profile_value = {
    'profile_version': PROFILE_VERSION,
    'cookies': [
      {
        'name': 'setup-profile',
        'value': 'setup-cookie',
        'domain': 'example.com',
        'path': '/',
        'expires': -1,
        'httpOnly': False,
        'secure': True,
        'sameSite': 'Lax',
      }
    ],
    'origins': [
      {
        'origin': 'https://example.com',
        'localStorage': [],
        'indexedDB': [
          {
            'name': 'setup-database',
            'version': 1,
            'stores': [
              {
                'name': 'records',
                'autoIncrement': False,
                'records': [{'key': 'login', 'value': 'indexed-db-session'}],
                'indexes': [],
              }
            ],
          }
        ],
      }
    ],
  }
  material.write_text(json.dumps(profile_value))

  output = _run_setup(store, _available_port())

  captured = json.loads(material.read_text())
  assert captured['cookies'][0]['value'] == 'setup-cookie'
  [database] = captured['origins'][0]['indexedDB']
  assert database['stores'][0]['records'] == [{'key': 'login', 'value': 'indexed-db-session'}]
  assert 'profile unchanged; wrote nothing' in output or '1 cookies across 1 sites' in output


def test_webview_open_is_denied_without_its_type_key(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='denied',
    source=_DENIED_ROUTE,
    vnc=False,
    observer=lambda _workspace, _route_ended: None,
    allowed=False,
  )
  assert code == 0
  assert (workspace.tree / '.webview-denied-report').read_text() == 'denied'


def test_cookies_pass_loads_profile_without_entering_the_owner_store(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  host_store = isolated_env.home / '.bro'
  profile_value = {
    'profile_version': PROFILE_VERSION,
    'cookies': [
      {
        'name': 'visible-profile',
        'value': 'visible-profile-cookie',
        'domain': 'example.com',
        'path': '/',
        'expires': -1,
        'httpOnly': False,
        'secure': True,
        'sameSite': 'Lax',
      },
      {
        'name': 'hidden-profile',
        'value': 'hidden-profile-cookie',
        'domain': 'example.com',
        'path': '/',
        'expires': -1,
        'httpOnly': True,
        'secure': True,
        'sameSite': 'Lax',
      },
    ],
    'origins': [
      {
        'origin': 'https://example.com',
        'localStorage': [{'name': 'profile-state', 'value': 'profile-local-storage'}],
      }
    ],
  }
  (host_store / 'creds' / 'cookies+e2e.cred').write_text(json.dumps(profile_value))
  monkeypatch.setattr(credentials, 'STORE_DIR', str(host_store))
  monkeypatch.setattr(credentials, '_default_store', None)

  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='profile',
    source=_PROFILE_ROUTE,
    vnc=False,
    observer=lambda _workspace, _route_ended: None,
    launch_scope={'webview': {'pass': frozenset({'cookies+e2e'})}},
  )

  assert code == 0, _diagnostic(workspace, '.webview-profile-error')
  report = json.loads((workspace.tree / '.webview-profile-report.json').read_text())
  assert 'visible-profile-cookie' in report['browser_state']['text']
  assert 'hidden-profile-cookie' not in report['request'].get('text', '')
  assert _live_containers(workspace) == []


def test_spawned_child_opens_a_granted_webview(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  import ride.bro_worker as ride_spawn

  original_started_party_launch = ride_spawn.started_party_launch

  def started_party_launch(*arguments, **keywords):
    launch = original_started_party_launch(*arguments, **keywords)
    return replace(launch, command=_session_broxy_probe(_SUMMON_WEBVIEW_CHILD))

  monkeypatch.setattr(ride_spawn, 'started_party_launch', started_party_launch)
  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='summoned',
    source=_SUMMON_WEBVIEW_ROUTE,
    vnc=False,
    observer=lambda _workspace, _route_ended: None,
    launch_scope={
      'bro': {'bros': frozenset({'bro'}), 'party': frozenset({'boxed'})},
      'webview': {},
    },
  )
  assert code == 0, _diagnostic(workspace, '.webview-summon-error')
  assert (workspace.tree / '.webview-summon-report').read_text() == 'opened'
  assert _live_containers(workspace) == []


def test_webview_peer_cannot_launch_without_its_own_type_key(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  from bro.webview.worker import WebviewType

  original_launch = WebviewType.launch

  def nested_launch(worker_type, request):
    run = original_launch(worker_type, request)
    return replace(
      run,
      spec=replace(run.spec, command=(_RUNTIME_PYTHON, '-c', _NESTED_WEBVIEW_WORKER)),
    )

  monkeypatch.setattr(WebviewType, 'launch', nested_launch)
  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='nested-denied',
    source=_NESTED_WEBVIEW_ROUTE,
    vnc=False,
    observer=lambda _workspace, _route_ended: None,
  )
  diagnostic = workspace.host_log.read_text() if workspace.host_log.is_file() else '(no host log)'
  assert code == 0, diagnostic
  assert (workspace.tree / '.webview-nested-report').read_text()
  assert _live_containers(workspace) == []


def test_real_webview_routes_commands_files_sharing_refusals_and_cleanup(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  env = isolated_env

  def inspect_files(workspace: Workspace, route_ended: threading.Event) -> None:
    ready = workspace.tree / '.webview-files-ready'
    _wait_until(
      lambda: ready.is_file() or route_ended.is_set(),
      1500,
      'webview screenshot checkpoint',
      None,
    )
    if not ready.is_file():
      return
    worker_directories = [
      directory
      for directory in workspace.path.parent.iterdir()
      if directory.name.startswith('webview-') and find_container_id(directory / 'tree') is not None
    ]
    assert len(worker_directories) == 1, worker_directories
    worker_tree = worker_directories[0] / 'tree'
    assert not (worker_tree / 'named.png').exists()
    output = worker_tree / 'output'
    assert not output.exists() or list(output.iterdir()) == []

    def files() -> set[str]:
      return {
        str(path.relative_to(worker_tree))
        for path in worker_tree.rglob('*')
        if path.is_file() and path.relative_to(worker_tree).parts[0] != 'artifacts'
      }

    baseline = files()
    (workspace.tree / '.webview-e2e-continue').touch()
    download_ready = workspace.tree / '.webview-download-ready'
    _wait_until(
      lambda: download_ready.is_file() or route_ended.is_set(),
      180,
      'refused download checkpoint',
      None,
    )
    if not download_ready.is_file():
      return
    assert files() == baseline
    (workspace.tree / '.webview-download-continue').touch()

  with _served_page(_NETWORK_PAGE) as page_url:
    code, workspace = _run_route(
      env,
      monkeypatch,
      suffix='full',
      source=_FULL_ROUTE.replace('__PAGE_URL__', repr(page_url)).replace(
        '__NETWORK_PAGE__', repr(_NETWORK_PAGE)
      ),
      vnc=False,
      observer=inspect_files,
    )

  diagnostic = _diagnostic(workspace, '.webview-e2e-error')
  assert code == 0, diagnostic
  report = json.loads((workspace.tree / '.webview-e2e-report.json').read_text())
  assert report['closed']['value']['commands'] >= 10
  assert len(report['artifact_refs']) == 3
  assert not any(
    directory.name.startswith('webview-') for directory in workspace.path.parent.iterdir()
  )
  assert _live_containers(workspace) == []


def test_vnc_open_answers_on_its_requested_loopback_port(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  port = _available_port()

  def probe_vnc(workspace: Workspace, route_ended: threading.Event) -> None:
    report = workspace.tree / '.webview-vnc-report.json'
    _wait_until(
      lambda: report.is_file() or route_ended.is_set(),
      1500,
      'webview VNC checkpoint',
      None,
    )
    if not report.is_file():
      return
    opened = json.loads(report.read_text())
    assert opened['vnc'].startswith(f'http://127.0.0.1:{port}/')
    with urllib.request.urlopen(opened['vnc'], timeout=15) as response:
      assert response.status == 200
      assert b'noVNC' in response.read()
    (workspace.tree / '.webview-vnc-continue').touch()

  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='vnc',
    source=_VNC_ROUTE.replace('__VNC_PORT__', str(port)),
    vnc=True,
    observer=probe_vnc,
  )

  assert code == 0, _diagnostic(workspace, '.webview-vnc-error')
  assert not any(
    directory.name.startswith('webview-') for directory in workspace.path.parent.iterdir()
  )
  assert _live_containers(workspace) == []


def test_plain_clean_reclaims_a_cancelled_webview_workspace(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='killed',
    source=_KILLED_ROUTE,
    vnc=False,
    observer=lambda _workspace, _route_ended: None,
  )

  assert code == 0, _diagnostic(workspace, '.webview-killed-error')
  worker_directories = [
    directory
    for directory in workspace.path.parent.iterdir()
    if directory.name.startswith('webview-')
  ]
  assert len(worker_directories) == 1, worker_directories
  worker = Workspace.open(worker_directories[0].name)
  assert worker.is_clean() == (True, [])
  assert list(worker.tree.iterdir()) == []
  assert _live_containers(workspace) == []

  monkeypatch.setattr(ride_clean, 'clean_managed_mirrors', lambda *_args, **_kwargs: (0, 0))
  monkeypatch.setattr(ride_clean, 'clean_runtime_bundles', lambda **_kwargs: (0, 0))
  assert ride_clean.clean_workspaces(names=[worker.name]) == 0
  assert not worker.path.exists()
