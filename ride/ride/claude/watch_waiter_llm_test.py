"""Live probes of `ride.claude.watch_waiter` against the pinned Claude Code.

The waiter rests on `asyncRewake` hooks, whose `rewakeSummary` and
`rewakeMessage` the settings schema marks internal, and on the waiter starting
from `StopFailure` as well as `Stop`; the runner rests on the stream's hook
events and on Claude holding a session's end for a pending async hook.
None of it is documented, so each route is held here: an idle wake in print
mode and in the TUI, a delivery inside a running turn, a waiter after a turn
that ends in an API error, a waiter that fails to start, and an end and a
stop that a pending waiter holds up neither of.
"""

import contextlib
import fcntl
import http.client
import http.server
import json
import os
import pty
import shlex
import signal
import ssl
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from bro import watches
from ride.claude.claude_argv import STREAM_JSON_ARGS, watch_waiter_hooks
from ride.claude.interrupt import StreamedRun, run_streaming
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  Feed,
  bounded,
  claude_token,
  pinned_claude,
  watched_session,
)
from ride.claude.waiter_state import WaiterState

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300.0
_AWAIT_SECONDS = 180.0
_POLL_SECONDS = 0.2
# Claude holds a print session's end this long for a pending async hook
_HOOK_GRACE_SECONDS = 30.0
_LINE = 'PROBE-LINE-4711'
_REPLY_WITH_THE_LINE = (
  'When a later message shows watch lines, reply with the line that contains PROBE-LINE, '
  'verbatim, and nothing else.'
)


@pytest.fixture(scope='module')
def claude() -> Path:
  return pinned_claude()


def _config(root: Path, *, user_hooks: dict | None = None) -> Path:
  """a Claude config dir for a session in `root`, whose user settings carry
  `user_hooks` beside the hooks a probe passes in `--settings`."""
  config = root / 'config'
  config.mkdir()
  state = {
    'hasCompletedOnboarding': True,
    'projects': {str(root): {'hasTrustDialogAccepted': True}},
  }
  (config / '.claude.json').write_text(json.dumps(state))
  settings = {'skipDangerousModePermissionPrompt': True, 'hooks': user_hooks or {}}
  (config / 'settings.json').write_text(json.dumps(settings))
  return config


def _environment(config: Path, **extra: str) -> dict[str, str]:
  environment = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  environment.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
    **extra,
  )
  return environment


def _waiter_hook() -> dict:
  return watch_waiter_hooks()['Stop'][0]['hooks'][0]


def _stream(
  claude: Path,
  root: Path,
  prompt: str,
  store: watches.Store,
  waiters: WaiterState,
  *,
  hooks: dict,
  on_result: Callable[[str], None],
  environment: dict[str, str] | None = None,
) -> StreamedRun:
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--dangerously-skip-permissions',
    '--settings',
    json.dumps({'hooks': hooks}, separators=(',', ':')),
    *STREAM_JSON_ARGS,
  ]
  env = environment if environment is not None else _environment(root / 'config')
  with contextlib.chdir(root), bounded(_SESSION_TIMEOUT_SECONDS):
    return run_streaming(argv, env, prompt, store=store, waiters=waiters, on_result=on_result)


def _transcript(config: Path) -> list[dict]:
  records = []
  for path in sorted((config / 'projects').rglob('*.jsonl')):
    records.extend(json.loads(line) for line in path.read_text().splitlines() if line != '')
  return records


def _texts(content: object) -> list[str]:
  if isinstance(content, str):
    return [content]
  if isinstance(content, list):
    return [
      block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text'
    ]
  return []


def _said(config: Path, role: str, text: str) -> list[str]:
  """every `role` message text in the session's transcript that contains `text`."""
  return [
    said
    for record in _transcript(config)
    if record.get('type') == role
    for said in _texts(record['message']['content'])
    if text in said
  ]


def _await(condition: Callable[[], bool], what: str) -> None:
  deadline = time.monotonic() + _AWAIT_SECONDS
  while not condition():
    assert time.monotonic() < deadline, f'{what} within {_AWAIT_SECONDS:.0f}s'
    time.sleep(_POLL_SECONDS)


class _Replies:
  """a run's `on_result`: hands the session `_LINE` at its first turn end and
  stops the feed once a reply carries it, which lets the session end."""

  def __init__(self, feed: Feed) -> None:
    self._feed = feed
    self._fed = False

  def __call__(self, reply: str) -> None:
    if _LINE in reply:
      self._feed.stop()
    elif not self._fed:
      self._fed = True
      self._feed.say(_LINE)


def test_an_idle_session_wakes_with_its_lines_under_the_waiters_labels(
  tmp_path: Path, claude: Path
) -> None:
  config = _config(tmp_path)
  with watched_session(tmp_path / 'session') as (store, waiters):
    replies = _Replies(Feed(store, tmp_path / 'lines'))
    run = _stream(
      claude,
      tmp_path,
      f'Reply with the single word READY and end your turn. {_REPLY_WITH_THE_LINE}',
      store,
      waiters,
      hooks=watch_waiter_hooks(),
      on_result=replies,
    )

  assert run.code == 0 and not run.stopped, run
  assert len(run.results) == 2 and _LINE in run.results[1], run
  hook = _waiter_hook()
  [wake] = _said(config, 'user', _LINE)
  assert f'<summary>{hook["rewakeSummary"]}</summary>' in wake
  assert f'{hook["rewakeMessage"]} \n' in wake
  assert 'Stop hook' not in wake


def test_a_line_arriving_mid_turn_is_delivered_within_that_turn(
  tmp_path: Path, claude: Path
) -> None:
  # the runner's notice over the background command starts the second turn, so
  # the waiter the first turn end started still waits through it; a PreToolUse
  # hook starts the watch that prints the line once that turn's foreground
  # command runs
  config = _config(tmp_path)
  feeder = tmp_path / 'feed.py'
  watch_run = shlex.join([sys.executable, '-m', 'bro.watch_run', 'echo', _LINE])
  feeder.write_text(
    'import json, subprocess, sys\n'
    'if json.load(sys.stdin)["tool_input"].get("command") == "sleep 8":\n'
    f'  subprocess.run({watch_run!r}, shell=True, check=True)\n'
  )
  hooks = {
    **watch_waiter_hooks(),
    'PreToolUse': [
      {
        'matcher': 'Bash',
        'hooks': [{'type': 'command', 'command': shlex.join([sys.executable, str(feeder)])}],
      }
    ],
  }
  prompt = (
    'Use the Bash tool with run_in_background set to true to run exactly: sleep 20. '
    'Do not wait for it. Reply with the single word ARMED and end your turn. '
    'When a message tells you the turn ended with work still live, use the Bash tool in the '
    'foreground (not in the background) to run exactly: sleep 8. Then reply with the line that '
    'contains PROBE-LINE from any watch message you saw, verbatim, or NONE, and end your turn. '
    'When the background command finishes, reply with the single word FINISHED.'
  )
  with watched_session(tmp_path / 'session') as (store, waiters):
    run = _stream(
      claude, tmp_path, prompt, store, waiters, hooks=hooks, on_result=lambda reply: None
    )

  assert run.code == 0 and not run.stopped, run
  # a turn of its own would have answered the line apart from the turn the
  # notice started; the three turns are the prompt's, the notice's, and the
  # finished command's
  assert len(run.results) == 3 and _LINE in run.results[1], run
  assert _said(config, 'user', _LINE) == [], 'the line opened a turn of its own'


def test_an_end_with_a_waiter_pending_skips_claudes_hook_grace(
  tmp_path: Path, claude: Path
) -> None:
  _config(tmp_path)
  with watched_session(tmp_path / 'session') as (store, waiters):
    ended: list[float] = []
    run = _stream(
      claude,
      tmp_path,
      'Reply with the single word READY.',
      store,
      waiters,
      hooks=watch_waiter_hooks(),
      on_result=lambda reply: ended.append(time.monotonic()),
    )
    exited = time.monotonic()
    assert waiters.registered(), 'no waiter started at the turn end'

  assert run.code == 0 and not run.stopped, run
  assert len(ended) == 1
  assert exited - ended[0] < _HOOK_GRACE_SECONDS / 3


def test_a_stop_with_a_waiter_pending_ends_the_run_as_stopped(tmp_path: Path, claude: Path) -> None:
  # claude kills a pending async hook on the interrupt rather than waiting for
  # it, which the runner must not read as the waiter failing
  _config(tmp_path)
  with watched_session(tmp_path / 'session') as (store, waiters):
    feed = Feed(store, tmp_path / 'lines')
    stopped: list[float] = []

    def _stop(reply: str) -> None:
      del reply
      _await(waiters.registered, 'no waiter started at the turn end')
      stopped.append(time.monotonic())
      os.kill(os.getpid(), signal.SIGTERM)

    run = _stream(
      claude,
      tmp_path,
      'Reply with the single word READY.',
      store,
      waiters,
      hooks=watch_waiter_hooks(),
      on_result=_stop,
    )
    exited = time.monotonic()
    feed.stop()

  assert run.stopped, run
  assert exited - stopped[0] < _HOOK_GRACE_SECONDS / 3


def test_a_waiter_that_fails_to_start_fails_the_solo_run(tmp_path: Path, claude: Path) -> None:
  # a package in the session's cwd shadows ride for the hook's `python -m`, as a
  # broken install would fail the waiter's import; the live feed would otherwise
  # hold the session waiting for a wake that never comes
  _config(tmp_path)
  shadow = tmp_path / 'ride'
  shadow.mkdir()
  (shadow / '__init__.py').write_text('raise ImportError("broken install")\n')
  with watched_session(tmp_path / 'session') as (store, waiters):
    Feed(store, tmp_path / 'lines')
    with pytest.raises(RuntimeError, match='(?s)the watch waiter failed with 1: .*broken install'):
      _stream(
        claude,
        tmp_path,
        'Reply with the single word READY.',
        store,
        waiters,
        hooks=watch_waiter_hooks(),
        on_result=lambda reply: None,
      )


class _FailingProxy(http.server.ThreadingHTTPServer):
  """the Anthropic API behind a loopback URL, failing every model request
  that carries `marker` until `recover()`."""

  def __init__(self, marker: bytes) -> None:
    super().__init__(('127.0.0.1', 0), _ProxyHandler)
    self.marker = marker
    self.recovered = threading.Event()

  @property
  def url(self) -> str:
    return f'http://127.0.0.1:{self.server_address[1]}'


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
  protocol_version = 'HTTP/1.1'
  _UPSTREAM = 'api.anthropic.com'
  _HOP_HEADERS = frozenset({'host', 'connection', 'accept-encoding'})
  _RELAYED_HEADERS_DROPPED = frozenset(
    {'transfer-encoding', 'connection', 'content-length', 'content-encoding'}
  )

  def log_message(self, format: str, *args: object) -> None:
    del format, args

  def _relay(self) -> None:
    proxy = self.server
    assert isinstance(proxy, _FailingProxy)
    length = int(self.headers.get('Content-Length') or 0)
    body = self.rfile.read(length) if length > 0 else None
    failing = (
      self.command == 'POST'
      and self.path.startswith('/v1/messages')
      and not proxy.recovered.is_set()
      and body is not None
      and proxy.marker in body
    )
    if failing:
      error = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'probe'}}
      payload = json.dumps(error).encode()
      self.send_response(400)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)
      return
    headers = {
      name: value for name, value in self.headers.items() if name.lower() not in self._HOP_HEADERS
    }
    upstream = http.client.HTTPSConnection(self._UPSTREAM, context=ssl.create_default_context())
    with contextlib.closing(upstream):
      upstream.request(self.command, self.path, body=body, headers=headers)
      response = upstream.getresponse()
      self.send_response(response.status)
      for name, value in response.getheaders():
        if name.lower() not in self._RELAYED_HEADERS_DROPPED:
          self.send_header(name, value)
      self.send_header('Transfer-Encoding', 'chunked')
      self.end_headers()
      while len(chunk := response.read1(65536)) > 0:
        self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
        self.wfile.flush()
      self.wfile.write(b'0\r\n\r\n')

  do_GET = do_POST = do_HEAD = _relay


@contextlib.contextmanager
def _failing_proxy(marker: str) -> Iterator[_FailingProxy]:
  proxy = _FailingProxy(marker.encode())
  thread = threading.Thread(target=proxy.serve_forever, daemon=True)
  thread.start()
  try:
    yield proxy
  finally:
    proxy.shutdown()
    thread.join()
    proxy.server_close()


def test_a_turn_ending_in_an_api_error_leaves_a_stop_failure_waiter(
  tmp_path: Path, claude: Path
) -> None:
  # a failing StopFailure hook of the user's own runs beside the waiter, and its
  # exit, which the stream reports as promptly as the waiter's, is no waiter failure
  failing = {'type': 'command', 'command': 'echo user hook >&2; exit 1', 'asyncRewake': True}
  config = _config(tmp_path, user_hooks={'StopFailure': [{'hooks': [failing]}]})
  marker = 'PROBE-API-ERROR'
  stop_failure_only = {'StopFailure': watch_waiter_hooks()['StopFailure']}
  with _failing_proxy(marker) as proxy, watched_session(tmp_path / 'session') as (store, waiters):
    feed = Feed(store, tmp_path / 'lines')

    def _on_result(reply: str) -> None:
      if _LINE in reply:
        feed.stop()
      else:
        proxy.recovered.set()
        feed.say(_LINE)

    run = _stream(
      claude,
      tmp_path,
      f'{marker}. Reply with the single word READY. {_REPLY_WITH_THE_LINE}',
      store,
      waiters,
      hooks=stop_failure_only,
      on_result=_on_result,
      environment=_environment(config, ANTHROPIC_BASE_URL=proxy.url),
    )

  assert run.code == 0 and not run.stopped, run
  assert len(run.results) == 2, run
  assert 'probe' in run.results[0]
  assert _LINE in run.results[1]


@contextlib.contextmanager
def _terminal(argv: list[str], env: dict[str, str], cwd: Path) -> Iterator[subprocess.Popen]:
  """run `argv` on a pty of its own, draining what it draws, until the block ends."""
  terminal, child_end = pty.openpty()
  fcntl.ioctl(child_end, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 160, 0, 0))
  process = subprocess.Popen(
    argv,
    env={**env, 'TERM': 'xterm-256color'},
    cwd=cwd,
    stdin=child_end,
    stdout=child_end,
    stderr=child_end,
    start_new_session=True,
  )
  os.close(child_end)

  def _drain() -> None:
    with contextlib.suppress(OSError):
      while len(os.read(terminal, 65536)) > 0:
        pass

  drain = threading.Thread(target=_drain, daemon=True)
  drain.start()
  try:
    yield process
  finally:
    process.terminate()
    try:
      process.wait(timeout=30)
    except subprocess.TimeoutExpired:
      process.kill()
      process.wait()
    os.close(terminal)
    drain.join()


def test_an_idle_tui_wakes_with_its_lines(tmp_path: Path, claude: Path) -> None:
  config = _config(tmp_path)
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--dangerously-skip-permissions',
    '--settings',
    json.dumps({'hooks': watch_waiter_hooks()}, separators=(',', ':')),
    f'Reply with the single word READY and end your turn. {_REPLY_WITH_THE_LINE}',
  ]
  with watched_session(tmp_path / 'session') as (store, waiters):
    feed = Feed(store, tmp_path / 'lines')
    with _terminal(argv, _environment(config), tmp_path):
      _await(lambda: len(_said(config, 'assistant', 'READY')) > 0, 'the TUI never replied')
      _await(waiters.registered, 'no waiter started at the turn end')
      feed.say(_LINE)
      _await(lambda: len(_said(config, 'assistant', _LINE)) > 0, 'the line never reached the model')
    feed.stop()

  [wake] = _said(config, 'user', _LINE)
  assert f'<summary>{_waiter_hook()["rewakeSummary"]}</summary>' in wake
