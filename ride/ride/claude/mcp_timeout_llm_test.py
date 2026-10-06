"""Live probe of the pinned unboxed runner's MCP idle and total backstops.

The session's HTTP MCP tool stays silent beyond Claude Code's native 300-second
idle cut and must still return through the environment the runner applies.
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
from unittest.mock import MagicMock, patch

import ride.claude.runner as runner
from bro import watches
from bro.llm.llms.claude_code import LLMSpec
from ride.claude.claude_argv import STREAM_JSON_ARGS, ClaudeLaunch
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  bounded,
  claude_token,
)
from ride.do_ride import SessionRun

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_TOOL_SILENCE_SECONDS = 305
_SESSION_TIMEOUT_SECONDS = 420
_RESULT = 'MCP-IDLE-BACKSTOP-PASSED'
_SERVER = f"""\
import time
from mcp.server.fastmcp import FastMCP

server = FastMCP(
  "fixture",
  host="127.0.0.1",
  port=int(__import__("sys").argv[1]),
  streamable_http_path="/mcp",
)


@server.tool()
def wait_past_idle_cut() -> str:
  time.sleep({_TOOL_SILENCE_SECONDS})
  return {_RESULT!r}


server.run(transport="streamable-http")
"""


def _available_port() -> int:
  with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    return int(listener.getsockname()[1])


def _wait_until_listening(process: subprocess.Popen[bytes], port: int) -> None:
  deadline = time.monotonic() + 30
  while True:
    if process.poll() is not None:
      raise RuntimeError(f'the MCP fixture exited during startup with {process.returncode}')
    try:
      with socket.create_connection(('127.0.0.1', port), timeout=0.2):
        return
    except OSError:
      pass
    if time.monotonic() >= deadline:
      raise RuntimeError('the MCP fixture did not listen within 30 seconds')
    time.sleep(0.05)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
  process.terminate()
  try:
    process.wait(timeout=10)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait()


@contextlib.contextmanager
def _slow_mcp_server(tmp_path: Path) -> Iterator[str]:
  script = tmp_path / 'slow_mcp.py'
  script.write_text(_SERVER)
  port = _available_port()
  process = subprocess.Popen(
    [sys.executable, str(script), str(port)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(_stop_process, process)
    _wait_until_listening(process, port)
    yield f'http://127.0.0.1:{port}/mcp'


def _environment(config: Path, session: Path) -> dict[str, str]:
  environment = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  environment.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
    RIDE_ISOLATION='unboxed',
    RIDE_RUNTIME=str(config.parent / 'runtime'),
    RIDE_SESSION_DIR=str(session),
    TRAILS_DISABLED='1',
  )
  return environment


def _session(prompt: str) -> SessionRun:
  return SessionRun(
    name='mcp-timeout-probe',
    repo=None,
    harness='claude',
    hold='unattended',
    llm=None,
    resolved_llm=LLMSpec(model='haiku').dump(),
    solo=True,
    resume=False,
    bro='bro',
    prompt=prompt,
    arguments=[],
  )


def test_silent_http_mcp_call_survives_the_native_idle_cut(tmp_path: Path, capfd) -> None:
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  session = tmp_path / 'session'
  session.mkdir()
  prompt = f'Call mcp__fixture__wait_past_idle_cut once, then reply exactly {_RESULT}.'

  with _slow_mcp_server(tmp_path) as url:
    mcp_config = json.dumps(
      {
        'mcpServers': {
          'fixture': {
            'type': 'http',
            'url': url,
            'alwaysLoad': True,
          }
        }
      },
      separators=(',', ':'),
    )
    launch = ClaudeLaunch(
      argv=[
        '--model',
        'haiku',
        '--allowedTools',
        'mcp__fixture__wait_past_idle_cut',
        '--dangerously-skip-permissions',
        '--max-turns',
        '3',
        '--mcp-config',
        mcp_config,
        *STREAM_JSON_ARGS,
      ],
      prompt=prompt,
    )
    server = MagicMock()
    server.endpoint.port = 1
    with (
      patch.dict(os.environ, _environment(config, session), clear=True),
      patch.object(runner, 'claude_projects_dir', return_value=tmp_path / 'projects'),
      patch.object(runner, 'start_session_mcp_server', return_value=server),
      patch.object(runner, 'build_claude_launch', return_value=launch),
      patch.object(runner, 'start_statusline_projector'),
      patch.object(runner, 'apply_claude_auth'),
      watches.Owner.for_session(),
      bounded(_SESSION_TIMEOUT_SECONDS),
    ):
      code = runner.run_session(_session(prompt))

  assert code == 0
  assert _RESULT in capfd.readouterr().out
