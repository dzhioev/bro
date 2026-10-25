import asyncio
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, BinaryIO, Optional, cast

import anyio
import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from bro.artifact import path_name
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.environment import BROKER_CHANNEL, BROKER_MISSION, BROKER_TALK
from bro.broker.transport import ChannelID
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.webview import profile, serve

TIMEOUT = 5.0
MISSION = 'webview-mission'
REF = 'sha256:' + 'a' * 64 + '/file'
SECRET_MARKER = 'profile-secret-marker'


def _profile(version: int = profile.PROFILE_VERSION) -> dict[str, Any]:
  return {
    'profile_version': version,
    'cookies': [{'name': 'session', 'value': SECRET_MARKER}],
    'origins': [
      {
        'origin': 'https://example.com',
        'localStorage': [{'name': 'signed-in', 'value': 'yes'}],
      }
    ],
  }


FAKE_MCP = r"""#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
from pathlib import Path

warmed = False
dynamic = False

def answer(identifier, result):
  print(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'result': result}), flush=True)

def content(text, error=False):
  return {'content': [{'type': 'text', 'text': text}], 'isError': error}

def tools():
  names = [
    'browser_navigate', 'echo', 'write', 'facts', 'tool_error',
    'schedule_exit', 'die', 'hang', 'browser_run_code_unsafe',
  ]
  if dynamic:
    names.append('page_dynamic_tool')
  return [
    {'name': name, 'description': f'{name} description', 'inputSchema': {'type': 'object'}}
    for name in names
  ]

for line in sys.stdin:
  message = json.loads(line)
  if 'id' not in message:
    continue
  identifier = message['id']
  method = message.get('method')
  if method == 'initialize':
    answer(identifier, {
      'protocolVersion': message['params']['protocolVersion'],
      'capabilities': {'tools': {}},
      'serverInfo': {'name': 'fake-playwright', 'version': '1'},
    })
    continue
  if method == 'tools/list':
    if not warmed:
      answer(identifier, {'tools': []})
    else:
      answer(identifier, {'tools': tools()})
    continue
  if method != 'tools/call':
    answer(identifier, {})
    continue
  name = message['params']['name']
  arguments = message['params'].get('arguments', {})
  if name == 'browser_navigate':
    if arguments == {'url': 'about:blank'}:
      warmed = True
      Path('output').mkdir(exist_ok=True)
      Path('output/warm-up.yml').write_text('warm-up snapshot')
      answer(identifier, content('warmed'))
    elif warmed:
      dynamic = True
      answer(identifier, content('navigated'))
    else:
      answer(identifier, content('browser was not warmed', True))
  elif not warmed:
    answer(identifier, content('browser was not warmed', True))
  elif name == 'echo':
    answer(identifier, content(arguments.get('text', '')))
  elif name == 'write':
    path = Path(arguments['path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(arguments['content'])
    answer(identifier, content(arguments.get('text', 'written')))
  elif name == 'facts':
    config_index = sys.argv.index('--config') + 1
    storage = None
    if '--storage-state' in sys.argv:
      storage_path = Path(sys.argv[sys.argv.index('--storage-state') + 1])
      storage = {
        'mode': oct(storage_path.stat().st_mode & 0o777),
        'value': json.loads(storage_path.read_text()),
      }
    answer(identifier, content(json.dumps({
      'cwd': os.getcwd(),
      'environment': dict(os.environ),
      'arguments': sys.argv[1:],
      'config': json.loads(Path(sys.argv[config_index]).read_text()),
      'storage': storage,
    }, sort_keys=True)))
  elif name == 'tool_error':
    answer(identifier, content(arguments.get('text', 'tool failed'), True))
  elif name == 'schedule_exit':
    answer(identifier, content('scheduled'))
    threading.Thread(target=lambda: (time.sleep(0.1), os._exit(19)), daemon=True).start()
  elif name == 'die':
    os._exit(17)
  elif name == 'hang':
    time.sleep(3600)
  else:
    answer(identifier, content(f'unknown tool {name}', True))
"""

X_SERVER = r"""#!/usr/bin/env python3
import signal
signal.pause()
"""

XVFB = r"""#!/usr/bin/env python3
import signal
print('73', flush=True)
signal.pause()
"""

PORT_SERVER = r"""#!/usr/bin/env python3
import signal
import socket
import sys
port = (
  int(sys.argv[sys.argv.index('-rfbport') + 1])
  if 'x11vnc' in sys.argv[0]
  else int(sys.argv[-2])
)
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('127.0.0.1', port))
sock.listen()
while True:
  connection, _ = sock.accept()
  connection.close()
"""


class BrokerSink:
  def __init__(self, workspace: Path):
    self.workspace = workspace
    self.messages: asyncio.Queue[tuple[ChannelID, Message]] = asyncio.Queue()
    self.transport: Optional[TcpServerTransport] = None
    self.minted: list[tuple[str, bytes]] = []
    self.refused_mints = False

  async def on_connect(self, channel: ChannelID) -> None:
    pass

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    if message.type == Tag.REQUEST and message.kind == 'artifact.mint':
      assert message.id is not None
      path = message.args['path']
      name = message.args.get('name', path_name(path))
      content = (self.workspace / path).read_bytes()
      self.minted.append((path, content))
      assert self.transport is not None
      if self.refused_mints:
        reply = brotocol.result(message.id, 'denied', error='artifact store cap reached')
      else:
        ref = f'sha256:{hashlib.sha256(content).hexdigest()}/{name}'
        reply = brotocol.result(message.id, 'ok', value={'ref': ref, 'size': len(content)})
      await self.transport.send(channel, reply)
      return
    await self.messages.put((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    pass

  async def next(
    self, predicate: Callable[[Message], bool] = lambda message: True
  ) -> tuple[ChannelID, Message]:
    while True:
      channel, message = await asyncio.wait_for(self.messages.get(), TIMEOUT)
      if predicate(message):
        return channel, message


@contextlib.asynccontextmanager
async def running_broker(monkeypatch, workspace: Path) -> AsyncIterator[BrokerSink]:
  transport = TcpServerTransport([LOCAL_HOST])
  sink = BrokerSink(workspace)
  sink.transport = transport
  task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)
  provisioned = await transport.provision()
  monkeypatch.setenv(BROKER_CHANNEL, provisioned.host_endpoint.address(LOCAL_HOST))
  try:
    yield sink
  finally:
    await transport.shutdown()
    await asyncio.wait_for(task, TIMEOUT)


def _executable(path: Path, content: str) -> Path:
  path.write_text(textwrap.dedent(content))
  path.chmod(path.stat().st_mode | stat.S_IXUSR)
  return path


@pytest.fixture
def daemon_files(tmp_path, monkeypatch, worker_id):
  worker_number = 0 if worker_id == 'master' else int(worker_id.removeprefix('gw'))
  port_base = 30000 + worker_number * 20
  monkeypatch.setattr(serve, 'X11_PORT', port_base)
  monkeypatch.setattr(serve, 'VNC_PORT', port_base + 1)
  workspace = tmp_path / 'workspace'
  workspace.mkdir()
  artifacts = workspace / 'artifacts'
  artifacts.mkdir()
  binaries = tmp_path / 'bin'
  binaries.mkdir()
  _executable(binaries / 'Xvfb', XVFB)
  _executable(binaries / 'x11vnc', PORT_SERVER)
  _executable(binaries / 'websockify', PORT_SERVER)
  playwright = _executable(binaries / 'playwright-mcp', FAKE_MCP)

  monkeypatch.setattr(serve, 'WORKSPACE', workspace)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', artifacts)
  monkeypatch.setattr(serve, 'OUTPUT_DIRECTORY', workspace / 'output')
  monkeypatch.setattr(serve, 'PLAYWRIGHT_COMMAND', str(playwright))
  monkeypatch.setattr(serve, '_require_artifact_view', lambda: None)
  monkeypatch.setattr(serve.credentials, 'try_get', lambda _kind: None)
  monkeypatch.setenv('PATH', f'{binaries}:{os.environ["PATH"]}')
  monkeypatch.setenv('HOME', str(tmp_path / 'home'))
  monkeypatch.setenv(serve.PLAYWRIGHT_BROWSERS_PATH_ENV, str(tmp_path / 'browsers'))
  monkeypatch.setenv(BROKER_MISSION, MISSION)
  monkeypatch.setenv(BROKER_TALK, 'owner.question,worker.say')
  monkeypatch.setenv(
    serve.OPTIONS_ENV,
    json.dumps({'vnc': False, 'downloads': False, 'allowed_origins': [], 'blocked_origins': []}),
  )
  return workspace, binaries


async def _ready(sink: BrokerSink) -> tuple[ChannelID, dict[str, Any]]:
  channel, listening = await sink.next(
    lambda message: message.type == Tag.MARK and message.payload.get('transition') == 'listening'
  )
  assert listening.request_id == MISSION
  ready_channel, ready = await sink.next(
    lambda message: message.type == Tag.MESSAGE and message.payload.get('event') == 'ready'
  )
  assert ready_channel == channel
  return channel, ready.payload


async def _ask(
  sink: BrokerSink,
  channel: ChannelID,
  payload: dict[str, Any],
  identifier: str = 'question',
) -> dict[str, Any]:
  assert sink.transport is not None
  await sink.transport.send(channel, brotocol.message(MISSION, payload, id=identifier))
  _, reply = await sink.next(
    lambda message: message.type == Tag.MESSAGE and message.reply_to == identifier
  )
  return reply.payload


@pytest.mark.parametrize(
  'raw',
  [
    'not-json',
    '[]',
    '{}',
    '{"vnc": 1, "downloads": false, "allowed_origins": [], "blocked_origins": []}',
    '{"vnc": false, "downloads": 1, "allowed_origins": [], "blocked_origins": []}',
    '{"vnc": false, "downloads": false, "allowed_origins": ["a;b"], "blocked_origins": []}',
  ],
)
def test_options_fail_fast_on_malformed_values(raw):
  with pytest.raises(serve.DaemonError):
    serve.decode_options(raw)


def test_profile_strips_its_version_and_reads_an_older_envelope(monkeypatch):
  monkeypatch.setattr(profile, 'PROFILE_VERSION', 2)

  assert profile.decode_profile(json.dumps(_profile(version=1))) == {
    'cookies': [{'name': 'session', 'value': SECRET_MARKER}],
    'origins': [
      {
        'origin': 'https://example.com',
        'localStorage': [{'name': 'signed-in', 'value': 'yes'}],
      }
    ],
  }


def test_profile_refuses_a_newer_version_naming_both_versions():
  newer = profile.PROFILE_VERSION + 1

  with pytest.raises(profile.ProfileError) as raised:
    profile.decode_profile(json.dumps(_profile(version=newer)))

  assert f'version {newer}' in str(raised.value)
  assert f'version {profile.PROFILE_VERSION}' in str(raised.value)
  assert SECRET_MARKER not in str(raised.value)


@pytest.mark.parametrize(
  'value',
  [
    f'not-json-{SECRET_MARKER}',
    json.dumps({**_profile(), 'sessionStorage': SECRET_MARKER}),
    json.dumps({**_profile(), 'origins': [{'origin': SECRET_MARKER, 'localStorage': {}}]}),
    *[
      (
        f'{{"profile_version":{profile.PROFILE_VERSION},'
        f'"cookies":[{{"value":"{SECRET_MARKER}"}},{constant}],"origins":[]}}'
      )
      for constant in ('NaN', 'Infinity', '-Infinity')
    ],
  ],
)
def test_profile_errors_name_the_defect_without_material(value):
  with pytest.raises(profile.ProfileError) as raised:
    profile.decode_profile(value)

  assert SECRET_MARKER not in str(raised.value)


def test_profile_bound_is_checked_before_parsing(monkeypatch):
  monkeypatch.setattr(profile, 'PROFILE_MAX_BYTES', 32)

  with pytest.raises(profile.ProfileError, match='32-byte limit'):
    profile.decode_profile('x' * 33)


def test_shared_child_stdout_stays_off_the_capture_exchange(monkeypatch):
  calls = []

  class Process:
    def poll(self):
      return 0

    def wait(self):
      return 0

  monkeypatch.setattr(
    serve.subprocess,
    'Popen',
    lambda command, **keywords: calls.append((command, keywords)) or Process(),
  )

  with serve._child('helper', ('helper', '--serve')):
    pass

  assert calls == [(('helper', '--serve'), {'stdout': sys.stderr})]


def test_mount_check_requires_the_declared_view_in_the_process_mount_table(tmp_path, monkeypatch):
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  monkeypatch.setattr(serve, 'MOUNTINFO', tmp_path / 'mountinfo')
  serve.ARTIFACT_VIEW.mkdir()
  serve.MOUNTINFO.write_text('')

  with pytest.raises(serve.DaemonError, match='/artifacts'):
    serve._require_artifact_view()


def test_mount_check_accepts_a_bind_from_the_same_device(tmp_path, monkeypatch):
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  monkeypatch.setattr(serve, 'MOUNTINFO', tmp_path / 'mountinfo')
  serve.ARTIFACT_VIEW.mkdir()
  serve.MOUNTINFO.write_text(
    f'42 31 8:1 /source {serve.ARTIFACT_VIEW} ro,relatime - ext4 /dev/root rw\n'
  )

  serve._require_artifact_view()


def test_published_port_parser_requires_the_vnc_mapping():
  assert serve._published_port(6080, '6080=49152,8080=49153') == 49152
  with pytest.raises(serve.DaemonError, match='no host mapping'):
    serve._published_port(6080, '8080=49153')
  with pytest.raises(serve.DaemonError, match='malformed'):
    serve._published_port(6080, 'bad')


@pytest.mark.asyncio
async def test_profile_reaches_playwright_as_private_storage_state(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  monkeypatch.setattr(
    serve.credentials,
    'try_get',
    lambda kind: json.dumps(_profile()) if kind == 'cookies' else None,
  )
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)

    facts = json.loads((await _ask(sink, channel, {'tool': 'facts'}, 'facts'))['text'])
    assert facts['storage'] == {
      'mode': '0o600',
      'value': {
        'cookies': [{'name': 'session', 'value': SECRET_MARKER}],
        'origins': [
          {
            'origin': 'https://example.com',
            'localStorage': [{'name': 'signed-in', 'value': 'yes'}],
          }
        ],
      },
    }
    assert facts['arguments'][0:2] == ['--isolated', '--storage-state']
    assert '--caps' not in facts['arguments']

    await _ask(sink, channel, {'webview': 'close'}, 'close')
    await sink.next(lambda message: message.type == Tag.RESULT)
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'raw',
  [
    f'not-json-{SECRET_MARKER}',
    json.dumps({**_profile(), 'sessionStorage': SECRET_MARKER}),
    json.dumps(_profile(version=profile.PROFILE_VERSION + 1)),
  ],
)
async def test_invalid_profile_fails_before_starting_the_display(daemon_files, monkeypatch, raw):
  monkeypatch.setattr(serve.credentials, 'try_get', lambda _kind: raw)
  monkeypatch.setattr(
    serve,
    '_xvfb',
    lambda: pytest.fail('display started before profile validation'),
  )

  with pytest.raises(serve.DaemonError) as raised:
    await serve.serve()

  assert SECRET_MARKER not in str(raised.value)


@pytest.mark.asyncio
async def test_oversize_profile_fails_before_starting_the_display(daemon_files, monkeypatch):
  monkeypatch.setattr(profile, 'PROFILE_MAX_BYTES', 32)
  monkeypatch.setattr(serve.credentials, 'try_get', lambda _kind: 'x' * 33)
  monkeypatch.setattr(
    serve,
    '_xvfb',
    lambda: pytest.fail('display started before profile validation'),
  )

  with pytest.raises(serve.DaemonError, match='32-byte limit'):
    await serve.serve()


@pytest.mark.asyncio
async def test_startup_warms_browser_then_listens_and_commands_reply_verbatim(
  daemon_files, monkeypatch
):
  workspace, _ = daemon_files
  monkeypatch.setenv(
    serve.OPTIONS_ENV,
    json.dumps(
      {
        'vnc': False,
        'downloads': False,
        'allowed_origins': ['https://allowed.example'],
        'blocked_origins': ['https://blocked.example'],
      }
    ),
  )
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, ready = await _ready(sink)
    assert ready == {'event': 'ready', 'vnc': None}
    assert not (workspace / 'output/warm-up.yml').exists()

    reply = await _ask(sink, channel, {'tool': 'echo', 'arguments': {'text': 'verbatim\ntext'}})
    assert reply == {'text': 'verbatim\ntext', 'files': []}

    facts = json.loads((await _ask(sink, channel, {'tool': 'facts'}, identifier='facts'))['text'])
    assert facts['cwd'] == str(workspace)
    assert set(facts['environment']) == {
      'DISPLAY',
      'HOME',
      'PATH',
      serve.PLAYWRIGHT_BROWSERS_PATH_ENV,
      'LC_CTYPE',
    }
    assert facts['environment']['DISPLAY'] == ':73'
    assert facts['config'] == {'browser': {'contextOptions': {'acceptDownloads': False}}}
    assert facts['storage'] is None
    assert '--storage-state' not in facts['arguments']
    assert '--caps' not in facts['arguments']
    assert ['--browser', 'chromium'] == facts['arguments'][2:4]
    assert facts['arguments'][-2:] == ['--blocked-origins', 'https://blocked.example']
    assert ['--allowed-origins', 'https://allowed.example'] == facts['arguments'][-4:-2]

    closed = await _ask(sink, channel, {'webview': 'close'}, identifier='close')
    assert closed == {'closed': True}
    _, result = await sink.next(lambda message: message.type == Tag.RESULT)
    assert result.payload == {'outcome': 'ok', 'value': {'commands': 2}}
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_command_files_are_minted_listed_and_removed(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  minted: list[tuple[str, bytes]] = []

  def mint(path: str):
    content = (workspace / path).read_bytes()
    minted.append((path, content))
    return SimpleMint(f'sha256:{hashlib.sha256(content).hexdigest()}/{path_name(path)}')

  monkeypatch.setattr(serve, 'mint_artifact', mint)
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)

    text = f'### Result\n- [Screenshot of viewport]({workspace / "nested/screenshot.png"})'
    reply = await _ask(
      sink,
      channel,
      {
        'tool': 'write',
        'arguments': {'path': 'nested/screenshot.png', 'content': 'image bytes', 'text': text},
      },
    )

    assert reply['text'] == text
    assert reply['files'] == [
      {
        'name': 'nested/screenshot.png',
        'ref': f'sha256:{hashlib.sha256(b"image bytes").hexdigest()}/screenshot.png',
      }
    ]
    assert minted == [('nested/screenshot.png', b'image bytes')]
    assert not (workspace / 'nested/screenshot.png').exists()

    await _ask(sink, channel, {'webview': 'close'}, identifier='close')
    await sink.next(lambda message: message.type == Tag.RESULT)
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_files_the_result_does_not_name_are_removed_without_minting(
  daemon_files, monkeypatch
):
  workspace, _ = daemon_files
  monkeypatch.setattr(serve, 'mint_artifact', lambda path: pytest.fail('mint should not run'))
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)

    reply = await _ask(
      sink,
      channel,
      {
        'tool': 'write',
        'arguments': {'path': 'scratch/notes.log', 'content': 'entries', 'text': 'written'},
      },
    )

    assert reply == {'text': 'written', 'files': []}
    assert not (workspace / 'scratch/notes.log').exists()

    await _ask(sink, channel, {'webview': 'close'}, identifier='close')
    await sink.next(lambda message: message.type == Tag.RESULT)
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_tools_malformed_withheld_and_tool_errors(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)

    tools = json.loads((await _ask(sink, channel, {'webview': 'tools'}, 'tools'))['text'])
    names = {tool['name'] for tool in tools}
    assert 'echo' in names
    assert 'browser_run_code_unsafe' not in names
    assert all(set(tool) == {'name', 'description', 'inputSchema'} for tool in tools)

    await _ask(
      sink,
      channel,
      {'tool': 'browser_navigate', 'arguments': {'url': 'https://dynamic.example'}},
      'navigate',
    )
    changed_tools = json.loads(
      (await _ask(sink, channel, {'webview': 'tools'}, 'changed-tools'))['text']
    )
    assert 'page_dynamic_tool' in {tool['name'] for tool in changed_tools}

    malformed = await _ask(sink, channel, {'tool': ''}, 'malformed')
    assert 'non-empty string' in malformed['error']
    withheld = await _ask(
      sink,
      channel,
      {'tool': 'browser_run_code_unsafe', 'arguments': {}},
      'withheld',
    )
    assert 'withholds' in withheld['error']
    tool_error = await _ask(
      sink,
      channel,
      {'tool': 'tool_error', 'arguments': {'text': 'page rejected the action'}},
      'error',
    )
    assert tool_error == {'error': 'page rejected the action'}

    await _ask(sink, channel, {'webview': 'close'}, 'close')
    _, result = await sink.next(lambda message: message.type == Tag.RESULT)
    assert result.payload['value'] == {'commands': 1}
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_downloads_option_sets_whether_the_browser_accepts_downloads(
  daemon_files, monkeypatch
):
  workspace, _ = daemon_files
  monkeypatch.setenv(
    serve.OPTIONS_ENV,
    json.dumps({'vnc': False, 'downloads': True, 'allowed_origins': [], 'blocked_origins': []}),
  )
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)

    facts = json.loads((await _ask(sink, channel, {'tool': 'facts'}, 'facts'))['text'])
    assert facts['config']['browser']['contextOptions']['acceptDownloads'] is True

    await _ask(sink, channel, {'webview': 'close'}, 'close')
    await sink.next(lambda message: message.type == Tag.RESULT)
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_vnc_starts_after_both_ports_and_reports_the_published_url(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  monkeypatch.setenv(
    serve.OPTIONS_ENV,
    json.dumps({'vnc': True, 'downloads': False, 'allowed_origins': [], 'blocked_origins': []}),
  )
  monkeypatch.setenv(serve.PUBLISHED_PORTS_ENV, f'{serve.VNC_PORT}=49152')
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, ready = await _ready(sink)
    assert ready['vnc'] == 'http://127.0.0.1:49152/vnc.html?autoconnect=1&resize=scale'

    await _ask(sink, channel, {'webview': 'close'}, 'close')
    await sink.next(lambda message: message.type == Tag.RESULT)
    await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_idle_playwright_exit_ends_the_daemon_with_process_status(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)
    assert (await _ask(sink, channel, {'tool': 'schedule_exit'}, 'schedule'))['text'] == 'scheduled'

    with pytest.raises(serve.DaemonError, match='Playwright MCP.*status 19'):
      await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_playwright_exit_mid_command_names_the_process(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)
    assert sink.transport is not None
    await sink.transport.send(
      channel,
      brotocol.message(MISSION, {'tool': 'die'}, id='die'),
    )

    with pytest.raises(serve.DaemonError, match='Playwright MCP.*status 17'):
      await asyncio.wait_for(daemon, TIMEOUT)


def _process_exiting_17_once_stdin_closes() -> subprocess.Popen:
  return subprocess.Popen(
    [sys.executable, '-c', 'import sys; sys.stdin.read(); sys.exit(17)'], stdin=subprocess.PIPE
  )


def _failed(error: Exception) -> asyncio.Future[Any]:
  future = asyncio.get_running_loop().create_future()
  future.set_exception(error)
  return future


@pytest.mark.asyncio
async def test_a_connection_closed_by_an_exiting_process_names_the_process():
  with _process_exiting_17_once_stdin_closes() as process:
    stdin = process.stdin
    assert stdin is not None
    async with serve._supervising([('Playwright MCP', process)]) as supervisor:
      # the process exits only after the operation has failed, so the supervisor
      # sees the closed connection before the exit
      closed = _failed(MCPError(code=mcp_types.CONNECTION_CLOSED, message='Connection closed'))
      closed.add_done_callback(lambda _: stdin.close())

      with pytest.raises(
        serve.DaemonError, match='Playwright MCP exited unexpectedly with status 17'
      ):
        await supervisor.run(closed, closed_process='Playwright MCP')


@pytest.mark.asyncio
async def test_other_protocol_errors_pass_through_the_supervisor():
  with _process_exiting_17_once_stdin_closes() as process:
    async with serve._supervising([('Playwright MCP', process)]) as supervisor:
      refused = _failed(MCPError(code=mcp_types.INVALID_PARAMS, message='bad arguments'))

      with pytest.raises(MCPError, match='bad arguments'):
        await supervisor.run(refused, closed_process='Playwright MCP')


@pytest.mark.asyncio
async def test_input_to_an_exited_process_reads_as_a_broken_stream():
  cancelled = SessionMessage(
    mcp_types.JSONRPCNotification(
      jsonrpc='2.0', method='notifications/cancelled', params={'requestId': 1}
    )
  )
  with subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE) as process:
    process.wait()
    assert process.stdin is not None
    stream = serve._WriteStream(cast(BinaryIO, process.stdin))

    with pytest.raises(anyio.BrokenResourceError):
      await stream.send(cancelled)
    await stream.aclose()
    with pytest.raises(anyio.ClosedResourceError):
      await stream.send(cancelled)


@pytest.mark.asyncio
async def test_stuck_command_ends_the_daemon_naming_the_command(daemon_files, monkeypatch):
  workspace, _ = daemon_files
  monkeypatch.setattr(serve, 'COMMAND_DEADLINE', 0.1)
  async with running_broker(monkeypatch, workspace) as sink:
    daemon = asyncio.create_task(serve.serve())
    channel, _ = await _ready(sink)
    assert sink.transport is not None
    await sink.transport.send(
      channel,
      brotocol.message(MISSION, {'tool': 'hang'}, id='hang'),
    )

    with pytest.raises(serve.DaemonError, match="command 'hang' exceeded"):
      await asyncio.wait_for(daemon, TIMEOUT)


@pytest.mark.asyncio
async def test_dead_vnc_process_fails_startup_before_ready(daemon_files, monkeypatch):
  workspace, binaries = daemon_files
  _executable(binaries / 'x11vnc', '#!/usr/bin/env python3\nraise SystemExit(23)\n')
  monkeypatch.setenv(
    serve.OPTIONS_ENV,
    json.dumps({'vnc': True, 'downloads': False, 'allowed_origins': [], 'blocked_origins': []}),
  )
  monkeypatch.setenv(serve.PUBLISHED_PORTS_ENV, f'{serve.VNC_PORT}=49152')
  async with running_broker(monkeypatch, workspace):
    with pytest.raises(serve.DaemonError, match='x11vnc.*status 23'):
      await asyncio.wait_for(serve.serve(), TIMEOUT)


@pytest.mark.asyncio
async def test_text_and_whole_reply_spills(monkeypatch, tmp_path):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  minted: list[bytes] = []

  def mint(path: str, name: str):
    content = (tmp_path / path).read_bytes()
    minted.append(content)
    return SimpleMint(f'sha256:{hashlib.sha256(content).hexdigest()}/{name}')

  monkeypatch.setattr(serve, 'mint_artifact', mint)
  text = 'x' * (serve.MAX_MESSAGE_BYTES + 1)
  text_reply = await serve.bound_reply({'text': text, 'files': []})
  assert text_reply == {
    'spilled': 'text',
    'ref': f'sha256:{hashlib.sha256(text.encode()).hexdigest()}/reply.txt',
    'bytes': len(text),
    'files': [],
  }

  files = [{'name': f'file-{index}', 'ref': REF} for index in range(400)]
  original = {'text': text, 'files': files}
  whole_reply = await serve.bound_reply(original)
  assert whole_reply['spilled'] == 'reply'
  assert json.loads(minted[-1]) == original


class SimpleMint:
  def __init__(self, ref: str):
    self.ref = ref


@pytest.mark.asyncio
async def test_spill_mint_refusal_is_a_bounded_error(monkeypatch, tmp_path):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)

  def refuse(path: str, name: str):
    raise serve.ArtifactError('store cap ' + 'x' * 5000)

  monkeypatch.setattr(serve, 'mint_artifact', refuse)
  dropped = 'z' * (serve.MAX_MESSAGE_BYTES + 1)
  reply = await serve.bound_reply({'text': dropped, 'files': []})

  assert reply['error'].startswith('store cap')
  assert len(reply['error']) == serve.ERROR_HEAD_CHARACTERS
  assert reply['dropped'] == len(dropped)
  assert len(serve._payload_bytes(reply)) <= serve.MAX_MESSAGE_BYTES


@pytest.mark.asyncio
async def test_command_file_limit_removes_every_output_without_minting(monkeypatch, tmp_path):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  monkeypatch.setattr(serve, 'OUTPUT_LIMIT_BYTES', 3)
  serve.ARTIFACT_VIEW.mkdir()
  before = serve.workspace_files()
  (tmp_path / 'one').write_bytes(b'12')
  (tmp_path / 'two').write_bytes(b'34')
  monkeypatch.setattr(serve, 'mint_artifact', lambda path: pytest.fail('mint should not run'))

  with pytest.raises(serve.DaemonError, match='holds 4 bytes'):
    await serve.collect_files(before, f'### Result\n- [One]({tmp_path / "one"})')

  assert not (tmp_path / 'one').exists()
  assert not (tmp_path / 'two').exists()


@pytest.mark.asyncio
async def test_refused_file_mint_is_reported_and_the_file_is_removed(monkeypatch, tmp_path):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  serve.ARTIFACT_VIEW.mkdir()
  before = serve.workspace_files()
  (tmp_path / 'large.bin').write_bytes(b'content')

  def refuse(path: str):
    raise serve.ArtifactError('artifact store cap reached')

  monkeypatch.setattr(serve, 'mint_artifact', refuse)
  files = await serve.collect_files(before, f'### Result\n- [Large]({tmp_path / "large.bin"})')

  assert files == [{'name': 'large.bin', 'error': 'artifact store cap reached'}]
  assert not (tmp_path / 'large.bin').exists()


@pytest.mark.asyncio
async def test_non_file_output_is_reported_and_removed(monkeypatch, tmp_path):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  serve.ARTIFACT_VIEW.mkdir()
  before = serve.workspace_files()
  (tmp_path / 'link').symlink_to('/etc/passwd')
  monkeypatch.setattr(serve, 'mint_artifact', lambda path: pytest.fail('mint should not run'))

  files = await serve.collect_files(before, f'### Result\n- [Link]({tmp_path / "link"})')

  assert files == [{'name': 'link', 'error': 'unsupported symlink output'}]
  assert not (tmp_path / 'link').exists()


@pytest.mark.asyncio
async def test_a_named_file_the_command_did_not_write_is_reported_and_left_alone(
  monkeypatch, tmp_path
):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  serve.ARTIFACT_VIEW.mkdir()
  (tmp_path / 'existing.txt').write_text('before the command')
  before = serve.workspace_files()
  monkeypatch.setattr(serve, 'mint_artifact', lambda path: pytest.fail('mint should not run'))
  text = f'### Result\n- [Existing]({tmp_path / "existing.txt"})\n- [Outside](/etc/passwd)'

  files = await serve.collect_files(before, text)

  assert files == [
    {'name': str(tmp_path / 'existing.txt'), 'error': 'named file was not written by the command'},
    {'name': '/etc/passwd', 'error': 'named file was not written by the command'},
  ]
  assert (tmp_path / 'existing.txt').read_text() == 'before the command'


# Result texts as Playwright MCP's response serializer lays them out:
# `### <section>` headings, link lines for written files, an Events line per download.
NAMED_FILES_CASES = {
  'screenshot': (
    '### Result\n- [Screenshot of viewport](/workspace/output/page-1.png)\n'
    '### Page\n- Page URL: https://example.test/',
    (['/workspace/output/page-1.png'], []),
  ),
  'snapshot written to a file': (
    '### Page\n- Page URL: https://example.test/\n### Snapshot\n- [Snapshot](/workspace/page.yml)',
    (['/workspace/page.yml'], []),
  ),
  'inline snapshot': (
    '### Snapshot\n```yaml\n- button "Open" [ref=e5]\n```',
    ([], []),
  ),
  'download': (
    '### Events\n- Downloading file report.bin ...\n'
    '- Downloaded file report.bin to "/workspace/output/report.bin"',
    ([], ['/workspace/output/report.bin']),
  ),
  'download still in progress': (
    '### Events\n- Downloading file report.bin ...',
    ([], []),
  ),
  'console entries are not an output': (
    '### Events\n- New console entries: /workspace/output/console-1.log#L1-L3',
    ([], []),
  ),
  'a link outside the file sections': (
    '### Open tabs\n- [Tab](/workspace/tab.png)\n### Page\n- [Page](/workspace/page.png)',
    ([], []),
  ),
  'a download line outside the events section': (
    '### Result\n- Downloaded file a.bin to "/workspace/output/a.bin"',
    ([], []),
  ),
  'a relative path': ('### Result\n- [Screenshot](./page.png)', ([], [])),
  'a repeated file': (
    '### Result\n- [Screenshot](/workspace/a.png)\n### Snapshot\n- [Snapshot](/workspace/a.png)',
    (['/workspace/a.png'], []),
  ),
  'a title with brackets and a path with parentheses': (
    '### Result\n- [Screenshot [full]](/workspace/shot (1).png)',
    (['/workspace/shot (1).png'], []),
  ),
  'a download name that contains the path delimiter': (
    '### Events\n- Downloaded file x to "/etc/passwd.bin to "/workspace/output/x.bin"',
    ([], ['/workspace/output/x.bin']),
  ),
}


def test_named_files_reads_a_long_malformed_line_in_linear_time():
  text = '### Result\n- [' + 'x](/' * 20000 + 'not-closed'
  started = time.monotonic()
  serve.named_files(text)
  assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
  ('text', 'expected'), NAMED_FILES_CASES.values(), ids=NAMED_FILES_CASES.keys()
)
def test_named_files_reads_the_paths_a_result_names(text, expected):
  written, downloaded = expected
  assert serve.named_files(text) == serve.NamedFiles(
    [Path(path) for path in written], [Path(path) for path in downloaded]
  )


@pytest.mark.asyncio
async def test_a_download_that_finished_between_commands_is_minted_when_a_reply_names_it(
  monkeypatch, tmp_path
):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  monkeypatch.setattr(serve, 'OUTPUT_DIRECTORY', tmp_path / 'output')
  serve.ARTIFACT_VIEW.mkdir()
  serve.OUTPUT_DIRECTORY.mkdir()
  (serve.OUTPUT_DIRECTORY / 'report.bin').write_bytes(b'report')
  before = serve.workspace_files()
  (serve.OUTPUT_DIRECTORY / 'console-1.log').write_text('entries')
  minted: list[str] = []

  def mint(path: str):
    minted.append(path)
    return SimpleMint(REF)

  monkeypatch.setattr(serve, 'mint_artifact', mint)
  text = f'### Events\n- Downloaded file report.bin to "{serve.OUTPUT_DIRECTORY / "report.bin"}"'

  files = await serve.collect_files(before, text)

  assert files == [{'name': 'output/report.bin', 'ref': REF}]
  assert minted == ['output/report.bin']
  assert not (serve.OUTPUT_DIRECTORY / 'report.bin').exists()
  assert (serve.OUTPUT_DIRECTORY / 'console-1.log').read_text() == 'entries'


@pytest.mark.asyncio
async def test_a_download_outside_the_output_directory_or_missing_is_reported_and_left_alone(
  monkeypatch, tmp_path
):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  monkeypatch.setattr(serve, 'OUTPUT_DIRECTORY', tmp_path / 'output')
  serve.ARTIFACT_VIEW.mkdir()
  (tmp_path / 'secret.txt').write_text('private')
  before = serve.workspace_files()
  monkeypatch.setattr(serve, 'mint_artifact', lambda path: pytest.fail('mint should not run'))
  text = (
    f'### Events\n- Downloaded file a to "{tmp_path / "secret.txt"}"\n'
    f'- Downloaded file b to "{tmp_path / "output/missing.bin"}"'
  )

  files = await serve.collect_files(before, text)

  assert [file['error'] for file in files] == ['named download is not in the output directory'] * 2
  assert (tmp_path / 'secret.txt').read_text() == 'private'


@pytest.mark.asyncio
async def test_output_limit_counts_a_download_that_finished_between_commands(monkeypatch, tmp_path):
  monkeypatch.setattr(serve, 'WORKSPACE', tmp_path)
  monkeypatch.setattr(serve, 'ARTIFACT_VIEW', tmp_path / 'artifacts')
  monkeypatch.setattr(serve, 'OUTPUT_DIRECTORY', tmp_path / 'output')
  monkeypatch.setattr(serve, 'OUTPUT_LIMIT_BYTES', 4)
  serve.ARTIFACT_VIEW.mkdir()
  serve.OUTPUT_DIRECTORY.mkdir()
  (serve.OUTPUT_DIRECTORY / 'large.bin').write_bytes(b'12345678')
  before = serve.workspace_files()
  monkeypatch.setattr(serve, 'mint_artifact', lambda path: pytest.fail('mint should not run'))
  text = f'### Events\n- Downloaded file large.bin to "{serve.OUTPUT_DIRECTORY / "large.bin"}"'

  with pytest.raises(serve.DaemonError, match='holds 8 bytes'):
    await serve.collect_files(before, text)

  assert not (serve.OUTPUT_DIRECTORY / 'large.bin').exists()
