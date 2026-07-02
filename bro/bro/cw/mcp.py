"""session-local HTTP MCP serving.

`--bro` and `--mcp local` sessions get their MCP tools from an
`mcp-server <spec> --http` instance the session owns — started by the container
entrypoint (fixed port, private netns) or by cw itself in host mode
(OS-assigned port, shared netns) — with claude pointed at it via a generated
`--mcp-config`.
"""

import json
import secrets
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base import spawn

# the port a session-local MCP server listens on inside a container. fixed: the
# container network namespace is private, so there is nothing to collide with.
# the value crosses the host/container boundary via CW_MCP_HTTP_PORT (cw builds
# the claude-config URLs host-side; the entrypoint starts the server).
_MCP_HTTP_PORT = 8300

# how long a host-mode server may take to bind. the bind needs only mcp-server's
# cheap module imports (the heavy ones are deferred past it), so this is
# generous headroom, not expected latency.
_BIND_TIMEOUT = 30.0


def _http_mcp_config(namespaces: list[str], *, port: int, token: str) -> str:
  """claude `--mcp-config` json: one `{type: http}` entry per namespace, mounted
  under the namespace as the server key so tools surface as `mcp__<ns>__<tool>`
  (the convention in prompts/tool_names.md)."""
  return json.dumps(
    {
      'mcpServers': {
        ns: {
          'type': 'http',
          'url': f'http://127.0.0.1:{port}/{ns}',
          'headers': {'Authorization': f'Bearer {token}'},
        }
        for ns in namespaces
      },
    },
    separators=(',', ':'),
  )


def _container_mcp_launch(spec: str, namespaces: list[str]) -> tuple[dict[str, str], str]:
  """(entrypoint env, claude mcp-config) for a session-local server in a container.

  the entrypoint reads CW_MCP_HTTP_SPEC / PORT / TOKEN to start
  `mcp-server <spec> --http` before it execs claude; the config points claude at
  the same port with the same per-session bearer token, one entry per namespace.
  """
  token = secrets.token_urlsafe(32)
  env = {
    'CW_MCP_HTTP_SPEC': spec,
    'CW_MCP_HTTP_PORT': str(_MCP_HTTP_PORT),
    'CW_MCP_HTTP_TOKEN': token,
  }
  return env, _http_mcp_config(namespaces, port=_MCP_HTTP_PORT, token=token)


@dataclass
class _HostMCPServer:
  """a session-local `mcp-server flow --http` owned by a host `--mcp local` session."""

  process: subprocess.Popen
  port: int
  token: str

  def mcp_config(self) -> str:
    return _http_mcp_config(['flow'], port=self.port, token=self.token)

  def stop(self) -> None:
    self.process.terminate()
    try:
      self.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
      self.process.kill()
      self.process.wait()


def _start_host_mcp_server(worktree: Path, env: Mapping[str, str]) -> _HostMCPServer:
  """start `mcp-server flow --http` on an OS-assigned port for a host session.

  `env` must carry the worktree venv's PATH: "local" means this checkout's flow
  code, so the console script — and the flow package it imports — resolve from
  the worktree, not the main repo. an OS-assigned port (`--port 0`) avoids
  collisions with concurrent host sessions in the shared netns; the server
  binds it before its heavy imports and publishes it via `--port-file`, so the
  wait here is milliseconds and a claude connect that lands mid-import sits in
  the TCP backlog until uvicorn accepts. runs in its own session (spawn.popen),
  outside the terminal's process group, so a Ctrl-C aimed at claude doesn't
  kill it; the caller stops it once claude exits. raises RuntimeError when the
  server dies or fails to bind in time.
  """
  token = secrets.token_urlsafe(32)
  state = Path(tempfile.mkdtemp(prefix='cw-mcp-'))
  port_file = state / 'port'
  log_path = state / 'server.log'
  with open(log_path, 'w') as log_file:
    process = spawn.popen(
      [
        'mcp-server',
        'flow',
        '--http',
        '--port',
        '0',
        '--port-file',
        str(port_file),
        '--bearer-token',
        token,
      ],
      cwd=str(worktree),
      env=dict(env),
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
      return _HostMCPServer(process, int(port_file.read_text()), token)
    if time.monotonic() >= deadline:
      process.terminate()
      raise RuntimeError(f'mcp-server did not bind within {_BIND_TIMEOUT:.0f}s; log: {log_path}')
    time.sleep(0.05)
