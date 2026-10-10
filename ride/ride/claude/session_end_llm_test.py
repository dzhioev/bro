"""Live probe of a session a service tool ends, against the pinned Claude Code.

The service tool signals the session's stop while its call is in flight, and the
`PostToolUse` hook `ride.claude.session_end` answers that call with
`continue: false`. Claude must then keep the call's own result, make no further
model call, and write the stopped turn with the record the stop waits for —
never the rejected result and user interrupt an interrupt leaves.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import ride.claude.interrupt as interrupt
from ride.claude.claude_argv import STREAM_JSON_ARGS, session_end_hooks
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  bounded,
  claude_token,
  watched_session,
)
from ride.claude.session_end_state import RAISE_TOOL, StoppedCallMark

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300.0
_SERVER = """\
import sys

from mcp.server.fastmcp import FastMCP

from ride.claude.harness import CLAUDE

server = FastMCP("bro", host="127.0.0.1", port=int(sys.argv[1]), streamable_http_path="/mcp")


@server.tool(name="raise")
async def raise_session(reason: str) -> str:
  return await CLAUDE.end_session(reason, "raised")


server.run(transport="streamable-http")
"""


def _available_port() -> int:
  with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    return int(listener.getsockname()[1])


def _stop_process(process: subprocess.Popen[bytes]) -> None:
  process.terminate()
  try:
    process.wait(timeout=10)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait()


@contextlib.contextmanager
def _service_server(tmp_path: Path) -> Iterator[str]:
  """the fixture serving `raise` through the Claude harness's own `end_session`,
  in the session environment the caller published."""
  script = tmp_path / 'service.py'
  script.write_text(_SERVER)
  port = _available_port()
  process = subprocess.Popen([sys.executable, str(script), str(port)], stdout=subprocess.DEVNULL)
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(_stop_process, process)
    deadline = time.monotonic() + 30
    while True:
      if process.poll() is not None:
        raise RuntimeError(f'the service fixture exited during startup with {process.returncode}')
      try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
          break
      except OSError:
        pass
      if time.monotonic() >= deadline:
        raise RuntimeError('the service fixture did not listen within 30 seconds')
      time.sleep(0.05)
    yield f'http://127.0.0.1:{port}/mcp'


def _session_env(config: Path) -> dict[str, str]:
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  env['CLAUDE_CODE_OAUTH_TOKEN'] = claude_token() or ''
  env['CLAUDE_CONFIG_DIR'] = str(config)
  env['DISABLE_AUTOUPDATER'] = '1'
  return env


def _text(content: object) -> str:
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    return ''.join(block.get('text', '') for block in content if isinstance(block, dict))
  return ''


def test_a_raise_keeps_its_result_and_stops_the_turn_it_ends(
  tmp_path: Path, claude: Path, monkeypatch
) -> None:
  monkeypatch.chdir(tmp_path)
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  prompt = (
    f'Call {RAISE_TOOL} exactly once with the reason "probe abort". '
    'That call ends the session; do nothing else.'
  )

  with watched_session(tmp_path / 'session') as (store, waiters):
    with _service_server(tmp_path) as url:
      # mounted under the bro service's namespace, so its `raise` is the tool the hook matches
      mcp_config = {'mcpServers': {'bro': {'type': 'http', 'url': url}}}
      argv = [
        str(claude),
        '--model',
        'haiku',
        '--allowedTools',
        RAISE_TOOL,
        '--dangerously-skip-permissions',
        '--strict-mcp-config',
        '--mcp-config',
        json.dumps(mcp_config),
        '--settings',
        json.dumps({'hooks': session_end_hooks()}),
        '--max-turns',
        '6',
        *STREAM_JSON_ARGS,
      ]
      with bounded(_SESSION_TIMEOUT_SECONDS):
        run = interrupt.run_streaming(
          argv, _session_env(config), prompt, store=store, waiters=waiters
        )
      stopped = StoppedCallMark.for_session().read()

  assert (run.code, run.stopped) == (0, True), run
  (transcript,) = (config / 'projects').rglob('*.jsonl')
  records = [json.loads(line) for line in transcript.read_text().splitlines()]
  assistants = [
    index
    for index, record in enumerate(records)
    if record.get('type') == 'assistant' and record['message'].get('model') != '<synthetic>'
  ]
  [(raised_at, call_id)] = [
    (index, block['id'])
    for index in assistants
    for block in records[index]['message']['content']
    if block.get('type') == 'tool_use' and block.get('name') == RAISE_TOOL
  ]
  assert assistants[-1] == raised_at, 'claude took a model call after the call that ended it'
  [result] = [
    block
    for record in records
    if record.get('type') == 'user' and isinstance(record['message'].get('content'), list)
    for block in record['message']['content']
    if block.get('type') == 'tool_result' and block.get('tool_use_id') == call_id
  ]
  assert result.get('is_error') is not True, result
  assert any(
    record.get('attachment', {}).get('type') == 'hook_stopped_continuation'
    and record['attachment'].get('toolUseID') == call_id
    for record in records
  )
  interrupts = [
    record
    for record in records
    if record.get('type') == 'user'
    and _text(record['message'].get('content')).startswith('[Request interrupted')
  ]
  assert interrupts == []
  assert stopped is not None and (stopped.call_id, stopped.transcript) == (call_id, transcript)
