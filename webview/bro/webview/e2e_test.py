"""Live broker, worker-container, Chromium, artifact, and VNC routes for webview."""

import contextlib
import json
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath

import pytest

import ride.workspace.docker as workspace_docker
import ride.workspace.host_docker_test_helper as host_docker
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
from ride.artifacts import view_mount
from ride.e2e_test import (
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
from ride.workspace.store import ScopedSecrets

pytestmark = host_docker.HOST_DAEMON_ONLY

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
  assert ':webview.vnc' in denied.stderr, denied.stderr

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

  remote = ask(
    mission,
    {'tool': 'browser_navigate', 'arguments': {'url': 'https://example.com/'}},
  )
  assert 'error' not in remote, remote
  requests = ask(
    mission,
    {
      'tool': 'browser_network_requests',
      'arguments': {'static': True, 'filter': 'example\\.com'},
    },
  )
  indexes = [int(value) for value in re.findall(r'(?m)^(\d+)\.', requests.get('text', ''))]
  assert indexes, requests
  response_body = ask(
    mission,
    {
      'tool': 'browser_network_request',
      'arguments': {
        'index': indexes[-1],
        'part': 'response-body',
        'filename': 'network-body.html',
      },
    },
  )
  body_file, body = one_file(response_body)
  assert body_file['name'] == 'network-body.html', body_file
  assert b'Example Domain' in body, body[:200]

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
    ['webview', 'open', '--vnc'], capture_output=True, text=True, check=True
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
  permits: set[str],
  observer: Callable[[Workspace, threading.Event], None],
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
      permits=permits,
      credential_scope=ScopedSecrets(set(), set()),
      container_runtime=container_runtime,
      runtime_bundle=runtime_bundle,
    )
  return code, workspace


def _diagnostic(workspace: Workspace, error_name: str) -> str:
  path = workspace.tree / error_name
  return path.read_text() if path.is_file() else 'the root wrote no error report'


def _live_containers(workspace: Workspace) -> list[str]:
  return [
    directory.name
    for directory in workspace.path.parent.iterdir()
    if find_container_id(directory / 'tree') is not None
  ]


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

  code, workspace = _run_route(
    env,
    monkeypatch,
    suffix='full',
    source=_FULL_ROUTE,
    permits=set(),
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


def test_vnc_open_answers_on_its_published_loopback_port(
  isolated_env: IsolatedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    with urllib.request.urlopen(opened['vnc'], timeout=15) as response:
      assert response.status == 200
      assert b'noVNC' in response.read()
    (workspace.tree / '.webview-vnc-continue').touch()

  code, workspace = _run_route(
    isolated_env,
    monkeypatch,
    suffix='vnc',
    source=_VNC_ROUTE,
    permits={'webview.vnc'},
    observer=probe_vnc,
  )

  assert code == 0, _diagnostic(workspace, '.webview-vnc-error')
  assert not any(
    directory.name.startswith('webview-') for directory in workspace.path.parent.iterdir()
  )
  assert _live_containers(workspace) == []
