"""Live probe that a Claude session's summon timeout reaches the summon client as
the model wrote it: seconds as a number and `none` as `'none'`.

The pinned release runs under the session's own settings, which decide how it
decodes an argument, and the call passes the session MCP server's check of the
arguments against the tool's advertised schema.
"""

import contextlib
import json
import os
import secrets
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from bro import summon
from bro.bro import _summon_tool
from bro.claude.claude_argv import STREAM_JSON_ARGS
from bro.claude.claude_config import _SESSION_SETTINGS_JSON
from bro.claude.live_claude_test_helper import REQUIRES_CLAUDE_CREDENTIAL, claude_token
from bro.claude.mcp import http_mcp_config
from bro.llm.mcp import InProcessMCPServer
from bro.runtime.mcp_server import create_http_app

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300
_PROMPT = (
  'Call mcp__bro__summon twice: once with target `dev`, prompt `bounded` and timeout '
  '14400, and once with target `dev`, prompt `unbounded` and timeout none. Then reply DONE.'
)


@contextlib.contextmanager
def _session_server() -> Iterator[str]:
  """the session MCP server's HTTP app serving the summon tool under `bro`, run in
  this process so the test's stub of the summon client holds; yields Claude's
  `--mcp-config` for it."""
  token = secrets.token_urlsafe(16)
  listener = socket.create_server(('127.0.0.1', 0))
  app = create_http_app([InProcessMCPServer('bro', [_summon_tool({}, None)])], token)
  server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
  # a connect before uvicorn accepts waits in the bound socket's backlog
  thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]})
  with contextlib.ExitStack() as stack:
    stack.enter_context(listener)
    stack.enter_context(contextlib.closing(app))
    thread.start()
    stack.callback(thread.join)
    stack.callback(setattr, server, 'should_exit', True)
    yield http_mcp_config(['bro'], port=listener.getsockname()[1], token=token)


def _environment(config: Path) -> dict[str, str]:
  environment = {
    name: value
    for name, value in os.environ.items()
    # the launch environment outranks the settings' own `env`
    if name
    not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY', 'DISABLE_TELEMETRY')
  }
  environment.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
  )
  return environment


def test_summon_takes_seconds_and_none_from_a_claude_session(
  claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  timeouts: dict[str, object] = {}

  def summon_detached(target: str, prompt: str, **kwargs: object) -> str:
    timeouts[prompt] = kwargs['timeout']
    return f'quest-{prompt}'

  monkeypatch.setattr(summon, 'summon_detached', summon_detached)
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  (config / 'settings.json').write_text(json.dumps(_SESSION_SETTINGS_JSON))
  message = {'type': 'user', 'message': {'role': 'user', 'content': _PROMPT}}

  with _session_server() as mcp_config:
    completed = subprocess.run(
      [
        str(claude),
        '--model',
        'haiku',
        '--strict-mcp-config',
        '--mcp-config',
        mcp_config,
        '--allowedTools',
        'mcp__bro__summon',
        '--dangerously-skip-permissions',
        '--max-turns',
        '4',
        *STREAM_JSON_ARGS,
      ],
      cwd=tmp_path,
      env=_environment(config),
      input=json.dumps(message) + '\n',
      capture_output=True,
      text=True,
      timeout=_SESSION_TIMEOUT_SECONDS,
    )

  assert completed.returncode == 0, completed.stderr
  assert timeouts == {'bounded': 14400, 'unbounded': 'none'}, completed.stdout
