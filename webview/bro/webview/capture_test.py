import contextlib
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bro.webview import capture, serve

_captured_seed: dict[str, Any] | None = None
_playwright_keywords: dict[str, Any] = {}
_playwright_session: Any = None


class Session:
  def __init__(self, state: dict | None = None, error: str | None = None):
    self.state = state
    self.error = error
    self.calls = []

  async def call_tool(self, name, arguments):
    self.calls.append((name, arguments))
    if name == 'browser_run_code_unsafe':
      if self.error is not None:
        return SimpleNamespace(isError=True, content=[SimpleNamespace(text=self.error)])
      path_match = re.search(r'path: (".*"), indexedDB: (true|false)', arguments['code'])
      assert path_match is not None
      path = Path(json.loads(path_match.group(1)))
      path.write_text(json.dumps(self.state))
    return SimpleNamespace(isError=False, content=[SimpleNamespace(text='ok')])


class Supervisor:
  async def run(self, awaitable, **_keywords):
    return await awaitable


@contextlib.contextmanager
def _xvfb():
  yield object(), ':73'


@contextlib.contextmanager
def _vnc(_display):
  yield (('x11vnc', object()), ('websockify', object()))


@contextlib.contextmanager
def _browser_config(seed):
  global _captured_seed
  _captured_seed = seed
  yield Path('/config'), None


@contextlib.asynccontextmanager
async def _playwright(*_arguments, **keywords):
  global _playwright_keywords
  _playwright_keywords = keywords
  yield _playwright_session, object()


@contextlib.asynccontextmanager
async def _supervising(_processes):
  yield Supervisor()


@pytest.fixture
def capture_runtime(monkeypatch):
  monkeypatch.setattr(serve, '_xvfb', _xvfb)
  monkeypatch.setattr(serve, '_vnc', _vnc)
  monkeypatch.setattr(serve, '_browser_config', _browser_config)
  monkeypatch.setattr(serve, '_playwright', _playwright)
  monkeypatch.setattr(serve, '_supervising', _supervising)
  monkeypatch.setattr(serve, '_warm_browser', lambda *_args: _async_none())
  monkeypatch.setattr(
    serve,
    '_content_text',
    lambda result: '\n'.join(item.text for item in result.content),
  )


async def _async_none():
  return None


def _state(indexed_db: bool = False):
  origin = {
    'origin': 'https://example.com',
    'localStorage': [{'name': 'signed-in', 'value': 'yes'}],
  }
  if indexed_db:
    origin['indexedDB'] = [{'name': 'auth', 'value': {'token': 'stored'}}]
  return {'cookies': [{'name': 'session', 'value': 'secret'}], 'origins': [origin]}


@pytest.mark.asyncio
async def test_capture_exchanges_seed_ready_request_and_state_in_order(
  capture_runtime, monkeypatch
):
  seed = _state()
  captured = _state(indexed_db=True)
  session = Session(captured)
  global _playwright_session
  _playwright_session = session
  monkeypatch.setenv(
    capture.CAPTURE_OPTIONS_ENV,
    json.dumps({'url': 'https://example.com', 'indexed_db': True}),
  )
  stdin = io.BytesIO(
    json.dumps({'seed': seed}).encode() + b'\n' + json.dumps({'request': 'state'}).encode() + b'\n'
  )
  stdout = io.BytesIO()

  await capture.capture(stdin, stdout)

  assert _captured_seed == seed
  assert _playwright_keywords['workspace'] == _playwright_keywords['output_directory'].parent
  lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
  assert lines == [{'event': 'ready'}, {'event': 'state', 'state': captured}]
  assert [name for name, _arguments in session.calls] == [
    'browser_navigate',
    'browser_run_code_unsafe',
  ]
  assert 'indexedDB: true' in session.calls[-1][1]['code']
  assert all(byte < 128 for byte in stdout.getvalue())


@pytest.mark.asyncio
async def test_navigation_infrastructure_failure_ends_capture(monkeypatch):
  session = Session()

  class DyingSupervisor:
    async def run(self, awaitable, **_keywords):
      awaitable.close()
      raise serve.DaemonError('x11vnc exited unexpectedly')

  with pytest.raises(serve.DaemonError, match='x11vnc'):
    await capture._navigate(session, cast(Any, DyingSupervisor()), 'https://example.com')


@pytest.mark.asyncio
async def test_supervised_exit_interrupts_the_state_request_wait(capture_runtime, monkeypatch):
  global _playwright_session
  _playwright_session = Session()
  monkeypatch.setenv(
    capture.CAPTURE_OPTIONS_ENV,
    json.dumps({'url': None, 'indexed_db': False}),
  )

  class DyingSupervisor:
    async def run(self, awaitable, **_keywords):
      awaitable.close()
      raise serve.DaemonError('Playwright MCP exited unexpectedly')

  @contextlib.asynccontextmanager
  async def supervising(_processes):
    yield DyingSupervisor()

  monkeypatch.setattr(serve, '_supervising', supervising)
  stdout = io.BytesIO()

  with pytest.raises(serve.DaemonError, match='Playwright MCP'):
    await capture.capture(io.BytesIO(b'{"seed":null}\n'), stdout)

  assert [json.loads(line) for line in stdout.getvalue().splitlines()] == [{'event': 'ready'}]


@pytest.mark.asyncio
async def test_capture_answers_a_tool_error_and_exits_nonzero_path(capture_runtime, monkeypatch):
  global _playwright_session
  _playwright_session = Session(error='browser closed')
  monkeypatch.setenv(
    capture.CAPTURE_OPTIONS_ENV,
    json.dumps({'url': None, 'indexed_db': False}),
  )
  stdin = io.BytesIO(b'{"seed":null}\n{"request":"state"}\n')
  stdout = io.BytesIO()

  with pytest.raises(capture.CaptureError, match='browser closed'):
    await capture.capture(stdin, stdout)

  assert [json.loads(line) for line in stdout.getvalue().splitlines()] == [
    {'event': 'ready'},
    {'event': 'error', 'error': 'Playwright storage-state capture failed: browser closed'},
  ]


@pytest.mark.asyncio
async def test_capture_rejects_an_oversize_state_before_sending_it(capture_runtime, monkeypatch):
  global _playwright_session
  monkeypatch.setattr(capture, 'PROFILE_MAX_BYTES', 32)
  _playwright_session = Session({'cookies': [{'value': 'x' * 64}], 'origins': []})
  monkeypatch.setenv(
    capture.CAPTURE_OPTIONS_ENV,
    json.dumps({'url': None, 'indexed_db': False}),
  )
  stdout = io.BytesIO()

  with pytest.raises(capture.CaptureError, match='exceeds'):
    await capture.capture(io.BytesIO(b'{"seed":null}\n{"request":"state"}\n'), stdout)

  assert json.loads(stdout.getvalue().splitlines()[-1])['event'] == 'error'


def test_capture_exchange_refuses_eof_wrong_order_and_oversize_lines(monkeypatch):
  with pytest.raises(capture.CaptureError, match='ended before seed'):
    capture._read_seed(io.BytesIO())
  with pytest.raises(capture.CaptureError, match='first line'):
    capture._read_seed(io.BytesIO(b'{"request":"state"}\n'))
  monkeypatch.setattr(capture, 'EXCHANGE_MAX_BYTES', 8)
  with pytest.raises(capture.CaptureError, match='line limit'):
    capture._read_line(io.BytesIO(b'{"seed":null}\n'), 'seed')


@pytest.mark.parametrize(
  'raw',
  [
    '{}',
    '{"url":null,"indexed_db":0}',
    '{"url":"","indexed_db":false}',
    '{"url":null,"indexed_db":false,"other":true}',
  ],
)
def test_capture_options_require_the_exact_shape(raw):
  with pytest.raises(capture.CaptureError):
    capture.decode_options(raw)
