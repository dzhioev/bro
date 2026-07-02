import json
import os
import textwrap
from pathlib import Path

import pytest

import cw.mcp


class TestHTTPMCPConfig:
  def test_one_entry_per_namespace(self):
    cfg = json.loads(cw.mcp._http_mcp_config(['flow', 'bro'], port=1234, token='tok'))
    assert list(cfg['mcpServers']) == ['flow', 'bro']
    for ns, entry in cfg['mcpServers'].items():
      assert entry['type'] == 'http'
      assert entry['url'] == f'http://127.0.0.1:1234/{ns}'
      assert entry['headers'] == {'Authorization': 'Bearer tok'}


class TestContainerMCPLaunch:
  def test_env_and_config_share_port_and_token(self):
    env, config = cw.mcp._container_mcp_launch('flow', ['flow'])
    assert env['CW_MCP_HTTP_SPEC'] == 'flow'
    assert env['CW_MCP_HTTP_PORT'] == str(cw.mcp._MCP_HTTP_PORT)
    entry = json.loads(config)['mcpServers']['flow']
    assert entry['url'] == f'http://127.0.0.1:{env["CW_MCP_HTTP_PORT"]}/flow'
    assert entry['headers'] == {'Authorization': f'Bearer {env["CW_MCP_HTTP_TOKEN"]}'}

  def test_token_is_per_launch(self):
    first, _ = cw.mcp._container_mcp_launch('flow', ['flow'])
    second, _ = cw.mcp._container_mcp_launch('flow', ['flow'])
    assert first['CW_MCP_HTTP_TOKEN'] != second['CW_MCP_HTTP_TOKEN']


def _fake_mcp_server(tmp_path: Path, body: str) -> dict[str, str]:
  """drop a fake `mcp-server` script on a private PATH and return the env for it.

  the script sees the real argv (`flow --http --port 0 --port-file <path>
  --bearer-token <token>`); `body` runs with $@ available.
  """
  bin_dir = tmp_path / 'bin'
  bin_dir.mkdir()
  script = bin_dir / 'mcp-server'
  script.write_text(f'#!/usr/bin/env bash\n{textwrap.dedent(body)}\n')
  script.chmod(0o755)
  return {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}


class TestStartHostMCPServer:
  def test_reads_port_from_port_file(self, tmp_path):
    env = _fake_mcp_server(
      tmp_path,
      """
      while [ "$1" != "--port-file" ]; do shift; done
      echo 45678 > "$2.tmp" && mv "$2.tmp" "$2"
      exec sleep 60
      """,
    )
    server = cw.mcp._start_host_mcp_server(tmp_path, env)
    try:
      assert server.port == 45678
      entry = json.loads(server.mcp_config())['mcpServers']['flow']
      assert entry['url'] == 'http://127.0.0.1:45678/flow'
      assert entry['headers'] == {'Authorization': f'Bearer {server.token}'}
    finally:
      server.stop()
    assert server.process.poll() is not None

  def test_startup_crash_raises(self, tmp_path):
    env = _fake_mcp_server(tmp_path, 'exit 3')
    with pytest.raises(RuntimeError, match='exited with code 3'):
      cw.mcp._start_host_mcp_server(tmp_path, env)

  def test_bind_timeout_raises_and_kills(self, tmp_path, monkeypatch):
    monkeypatch.setattr(cw.mcp, '_BIND_TIMEOUT', 0.3)
    env = _fake_mcp_server(tmp_path, 'exec sleep 60')
    with pytest.raises(RuntimeError, match='did not bind'):
      cw.mcp._start_host_mcp_server(tmp_path, env)
