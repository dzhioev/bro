import json
import os
import textwrap
from pathlib import Path

import pytest

import ride.claude.mcp as ride_mcp


class TestHTTPMCPConfig:
  def test_one_entry_per_namespace(self):
    config = json.loads(ride_mcp.http_mcp_config(['flow', 'bro'], port=1234, token='tok'))
    assert list(config['mcpServers']) == ['flow', 'bro']
    for namespace, entry in config['mcpServers'].items():
      assert entry['type'] == 'http'
      assert entry['url'] == f'http://127.0.0.1:1234/{namespace}'
      assert entry['headers'] == {'Authorization': 'Bearer tok'}
      assert entry['alwaysLoad'] is True


@pytest.fixture
def fake_mcp_server(tmp_path: Path, monkeypatch):
  """a factory standing a fake `mcp-server` in for the console script the runner
  resolves, returning the env to launch the server with.

  the script sees the real argv (`<spec> --http --port 0 --port-file <path>`)
  and inherited bearer-token environment; `body` runs with $@ available.
  """

  def _install(body: str) -> dict[str, str]:
    script = tmp_path / 'mcp-server'
    script.write_text(f'#!/usr/bin/env bash\n{textwrap.dedent(body)}\n')
    script.chmod(0o755)
    monkeypatch.setattr(ride_mcp.spawn, 'console_script', lambda name: str(script))
    return dict(os.environ)

  return _install


def _path_without(command: str) -> str:
  """the process PATH with every directory holding `command` dropped."""
  entries = os.environ['PATH'].split(os.pathsep)
  return os.pathsep.join(entry for entry in entries if not (Path(entry) / command).exists())


# fake-server body: binds a real HTTP socket that answers /health with the given
# status, publishing the OS-assigned port through the --port-file protocol
_HEALTH_SERVER_BODY = """
    while [ "$1" != "--port-file" ]; do shift; done
    exec python3 - "$2" <<'EOF'
    import http.server, os, sys

    class Handler(http.server.BaseHTTPRequestHandler):
      def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.end_headers()

      def log_message(self, *args):
        pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port_file = sys.argv[1]
    with open(port_file + '.tmp', 'w') as f:
      f.write(str(server.server_address[1]))
    os.rename(port_file + '.tmp', port_file)
    server.serve_forever()
    EOF
    """


class TestStartSessionMCPServer:
  def test_reads_port_from_port_file(self, tmp_path, fake_mcp_server):
    env = fake_mcp_server(
      """
      while [ "$1" != "--port-file" ]; do shift; done
      echo 45678 > "$2.tmp" && mv "$2.tmp" "$2"
      exec sleep 60
      """,
    )
    server = ride_mcp.start_session_mcp_server('flow', tmp_path, env)
    try:
      assert server.endpoint.port == 45678
      assert len(server.endpoint.token) > 0
    finally:
      server.stop()
    assert server.process.poll() is not None

  def test_passes_spec_on_argv_and_token_in_environment(self, tmp_path, fake_mcp_server):
    env = fake_mcp_server(
      f"""
      printf '%s\n' "$@" > {tmp_path}/argv
      printf '%s' "$MCP_SERVER_BEARER_TOKEN" > {tmp_path}/token
      while [ "$1" != "--port-file" ]; do shift; done
      echo 1 > "$2.tmp" && mv "$2.tmp" "$2"
      exec sleep 60
      """,
    )
    server = ride_mcp.start_session_mcp_server('bro:dev', tmp_path, env)
    server.stop()
    argv = (tmp_path / 'argv').read_text().splitlines()
    assert argv[0] == 'bro:dev'
    assert '--bearer-token' not in argv
    assert server.endpoint.token not in argv
    assert (tmp_path / 'token').read_text() == server.endpoint.token

  def test_starts_with_the_server_command_absent_from_path(self, tmp_path, fake_mcp_server):
    env = fake_mcp_server(
      """
      while [ "$1" != "--port-file" ]; do shift; done
      echo 1 > "$2.tmp" && mv "$2.tmp" "$2"
      exec sleep 60
      """,
    )
    env['PATH'] = _path_without(ride_mcp.MCP_SERVER_COMMAND)
    server = ride_mcp.start_session_mcp_server('flow', tmp_path, env)
    server.stop()

  def test_startup_crash_raises(self, tmp_path, fake_mcp_server):
    env = fake_mcp_server('exit 3')
    with pytest.raises(RuntimeError, match='exited with code 3'):
      ride_mcp.start_session_mcp_server('flow', tmp_path, env)

  def test_bind_timeout_raises_and_kills(self, tmp_path, monkeypatch, fake_mcp_server):
    monkeypatch.setattr(ride_mcp, '_BIND_TIMEOUT', 0.3)
    env = fake_mcp_server('exec sleep 60')
    with pytest.raises(RuntimeError, match='did not bind'):
      ride_mcp.start_session_mcp_server('flow', tmp_path, env)


class TestWaitHealthy:
  def test_returns_once_health_answers(self, tmp_path, fake_mcp_server):
    env = fake_mcp_server(_HEALTH_SERVER_BODY)
    server = ride_mcp.start_session_mcp_server('bro:dev', tmp_path, env)
    try:
      server.wait_healthy()
    finally:
      server.stop()

  def test_times_out_when_health_never_answers(self, tmp_path, monkeypatch, fake_mcp_server):
    monkeypatch.setattr(ride_mcp, '_HEALTH_TIMEOUT', 0.3)
    # binds and publishes the port but never serves HTTP, so /health can't answer
    env = fake_mcp_server(
      """
      while [ "$1" != "--port-file" ]; do shift; done
      echo 45678 > "$2.tmp" && mv "$2.tmp" "$2"
      exec sleep 60
      """,
    )
    server = ride_mcp.start_session_mcp_server('bro:dev', tmp_path, env)
    try:
      with pytest.raises(RuntimeError, match='not healthy'):
        server.wait_healthy()
    finally:
      server.stop()

  def test_server_death_raises(self, tmp_path, fake_mcp_server):
    env = fake_mcp_server(
      """
      while [ "$1" != "--port-file" ]; do shift; done
      echo 45678 > "$2.tmp" && mv "$2.tmp" "$2"
      sleep 0.1
      """,
    )
    server = ride_mcp.start_session_mcp_server('bro:dev', tmp_path, env)
    try:
      with pytest.raises(RuntimeError, match='before /health'):
        server.wait_healthy()
    finally:
      server.stop()
