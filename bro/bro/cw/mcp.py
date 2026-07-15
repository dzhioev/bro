"""session-local HTTP MCP serving.

Every `cw ss` session gets its MCP tools from an `mcp-server <spec> --http`
instance the in-place session runner owns — OS-assigned port published via a
port file, per-session bearer token — with claude pointed at it via a generated
`--mcp-config`.
"""

import json
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base import spawn
from mcp_server import BEARER_TOKEN_ENV

# how long a runner-owned server may take to bind. the bind needs only
# mcp-server's cheap module imports (the heavy ones are deferred past it), so
# this is generous headroom, not expected latency.
_BIND_TIMEOUT = 30.0

# how long /health may take to answer after the bind: the full tool import
# (bro graph → flow → notion → boto3) runs behind it, seconds in practice.
_HEALTH_TIMEOUT = 60.0


@dataclass(frozen=True)
class MCPEndpoint:
  """where a session-local MCP server listens; the claude `--mcp-config` (one URL
  per namespace) is built from it by `cw.claude_argv`."""

  port: int
  token: str


def _server_entry(url: str, token: str) -> dict:
  """one server entry of a claude `--mcp-config`. `alwaysLoad` makes claude block
  startup until the server is connected: its MCP connects are otherwise async and
  don't block an immediately-submitted prompt, so without it the first API request
  can be built with no tools attached — leaving the model to act on a system
  prompt full of tools it cannot call."""
  return {
    'type': 'http',
    'url': url,
    'headers': {'Authorization': f'Bearer {token}'},
    'alwaysLoad': True,
  }


def _http_mcp_config(namespaces: list[str], *, port: int, token: str) -> str:
  """claude `--mcp-config` json: one `{type: http}` entry per namespace, mounted
  under the namespace as the server key so tools surface as `mcp__<namespace>__<tool>`
  (the convention in prompts/tool_names.md)."""
  return json.dumps(
    {
      'mcpServers': {
        namespace: _server_entry(f'http://127.0.0.1:{port}/{namespace}', token)
        for namespace in namespaces
      },
    },
    separators=(',', ':'),
  )


@dataclass
class _SessionMCPServer:
  """a session-local `mcp-server <spec> --http` owned by the launching session."""

  process: subprocess.Popen
  endpoint: MCPEndpoint
  log_path: Path

  def wait_healthy(self) -> None:
    """block until /health answers 200 — every namespace endpoint ready to serve.

    the runner gates the claude launch on this so the multi-second bro import
    is paid here, off claude's critical path: claude itself blocks startup on
    the server's connect (the `alwaysLoad` config entries), and that block must
    not spend its connect timeout waiting out our import. raises RuntimeError
    when the server dies or the deadline passes.
    """
    url = f'http://127.0.0.1:{self.endpoint.port}/health'
    deadline = time.monotonic() + _HEALTH_TIMEOUT
    while True:
      if self.process.poll() is not None:
        raise RuntimeError(
          f'mcp-server exited with code {self.process.returncode} before /health; '
          f'log: {self.log_path}'
        )
      try:
        with urllib.request.urlopen(url, timeout=1) as response:
          if response.status == 200:
            return
      except (urllib.error.URLError, TimeoutError):
        pass
      if time.monotonic() >= deadline:
        raise RuntimeError(
          f'mcp-server not healthy within {_HEALTH_TIMEOUT:.0f}s; log: {self.log_path}'
        )
      time.sleep(0.2)

  def stop(self) -> None:
    self.process.terminate()
    try:
      self.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
      self.process.kill()
      self.process.wait()


def _start_session_mcp_server(spec: str, cwd: Path, env: Mapping[str, str]) -> _SessionMCPServer:
  """start `mcp-server <spec> --http` on an OS-assigned port for a session.

  `env` must carry the workspace venv's PATH: the tools serve this workspace's
  code, so the console script — and the packages it imports — resolve from the
  workspace, not the launching repo. an OS-assigned port (`--port 0`) avoids
  collisions with concurrent sessions in a shared netns; the server binds it
  before its heavy imports and publishes it via `--port-file`, so the wait here
  is milliseconds and a claude connect that lands mid-import sits in the TCP
  backlog until uvicorn accepts. runs in its own session (spawn.popen), outside
  the terminal's process group, so a Ctrl-C aimed at claude doesn't kill it; the
  caller stops it once claude exits and gates the launch on `wait_healthy`.
  raises RuntimeError when the server dies or fails to bind in time.
  """
  token = secrets.token_urlsafe(32)
  state = Path(tempfile.mkdtemp(prefix='cw-mcp-'))
  port_file = state / 'port'
  log_path = state / 'server.log'
  server_env = dict(env)
  server_env[BEARER_TOKEN_ENV] = token
  with open(log_path, 'w') as log_file:
    process = spawn.popen(
      [
        'mcp-server',
        spec,
        '--http',
        '--port',
        '0',
        '--port-file',
        str(port_file),
      ],
      cwd=str(cwd),
      env=server_env,
      stdout=log_file,
      stderr=subprocess.STDOUT,
    )
  deadline = time.monotonic() + _BIND_TIMEOUT
  while True:
    if process.poll() is not None:
      raise RuntimeError(
        f'mcp-server exited with code {process.returncode} during startup; log: {log_path}'
      )
    if port_file.exists():
      endpoint = MCPEndpoint(port=int(port_file.read_text()), token=token)
      return _SessionMCPServer(process, endpoint, log_path)
    if time.monotonic() >= deadline:
      process.terminate()
      raise RuntimeError(f'mcp-server did not bind within {_BIND_TIMEOUT:.0f}s; log: {log_path}')
    time.sleep(0.05)
