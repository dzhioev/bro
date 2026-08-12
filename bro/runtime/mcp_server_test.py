import importlib.metadata
from typing import ClassVar

import pytest
from starlette.testclient import TestClient

from bro.bro import BaseBro
from bro.datasources.searchable import Hit, SearchableDataSource
from bro.llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, MCPServerSpec, describe
from bro.runtime import mcp_server
from bro.runtime.mcp_server import _resolve_servers, create_http_app

TOKEN = 'test-bearer-token'
_MCP_HEADERS = {
  'Authorization': f'Bearer {TOKEN}',
  'Accept': 'application/json, text/event-stream',
}


class _NoopSource(SearchableDataSource):
  name = 'noop'
  summary = 'no-op data source'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def _fetch_content(self, id: str) -> str:
    return ''


def _ping() -> str:
  return 'pong'


describe(_ping, 'ping the noop server')


def _create_ping_server() -> MCPServer:
  return InProcessMCPServer('ping', [FunctionTool(_ping)])


def _ping_toolset() -> MCPServerSpec:
  return MCPServerSpec(build=_create_ping_server)


def _entry_point(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, mcp_server._TOOLSET_ENTRY_POINT_GROUP)


class _ShimBro(BaseBro):
  name = 'shim-test'
  description = 'composes a ping server and a data source'
  data_sources: ClassVar = [_NoopSource()]
  tools: ClassVar = [MCPServerSpec(build=_create_ping_server)]

  def __init__(self):
    super().__init__(system_prompt='test')


class _SecondSource(SearchableDataSource):
  name = 'second'
  summary = 'another no-op data source'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def _fetch_content(self, id: str) -> str:
    return ''


class _TwoSourceBro(BaseBro):
  name = 'two-source'
  description = 'two searchable sources that both expose search/fetch'
  data_sources: ClassVar = [_NoopSource(), _SecondSource()]

  def __init__(self):
    super().__init__(system_prompt='test')


def _client(servers: list[MCPServer]) -> TestClient:
  return TestClient(create_http_app(servers, TOKEN), raise_server_exceptions=False)


def _rpc(client: TestClient, path: str, method: str, params=None, headers=None) -> dict:
  payload = {'jsonrpc': '2.0', 'id': 1, 'method': method}
  if params is not None:
    payload['params'] = params
  response = client.post(
    path, json=payload, headers=headers if headers is not None else _MCP_HEADERS
  )
  assert response.status_code == 200, response.text
  return response.json()


class TestResolveServers:
  def test_contributed_toolset_is_discovered(self, monkeypatch):
    monkeypatch.setattr(
      mcp_server,
      '_toolset_entry_points',
      lambda: (_entry_point('ping', 'bro.runtime.mcp_server_test:_ping_toolset'),),
    )
    servers = _resolve_servers('ping')
    assert len(servers) == 1
    assert servers[0].namespace == 'ping'

  def test_toolset_entry_points_use_the_expected_group(self, monkeypatch):
    calls = []

    def entry_points(**kwargs):
      calls.append(kwargs)
      return ()

    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    assert mcp_server._toolset_entry_points() == ()
    assert calls == [{'group': 'bro.toolsets'}]

  def test_absent_toolset_has_a_clear_error(self, monkeypatch):
    monkeypatch.setattr(mcp_server, '_toolset_entry_points', lambda: ())
    with pytest.raises(SystemExit, match="unknown server 'tasks'; expected one of"):
      _resolve_servers('tasks')

  def test_contributed_brog(self, monkeypatch):
    # brog's state factory reads the self-contained config at build time
    from bro.base import credentials

    monkeypatch.setattr(
      credentials,
      'get_json',
      lambda name: {'backend': 'github', 'token': 't', 'repo': 'owner/repository'},
    )
    servers = _resolve_servers('brog')
    assert len(servers) == 1
    assert servers[0].namespace == 'brog'

  def test_unknown(self):
    with pytest.raises(SystemExit, match='unknown server'):
      _resolve_servers('does-not-exist')

  def test_bro_spec_includes_service_namespace(self):
    namespaces = {server.namespace for server in _resolve_servers('bro:bro')}
    assert 'bro' in namespaces


class TestHTTPBindBeforeResolve:
  def test_port_file_published_before_server_resolution(self, tmp_path, monkeypatch):
    port_file = tmp_path / 'port'

    def resolve(spec):
      # the bind + port-file publish must precede the heavy tool resolution
      assert port_file.exists()
      raise RuntimeError('resolution reached')

    monkeypatch.setattr(mcp_server, '_resolve_servers', resolve)
    with pytest.raises(RuntimeError, match='resolution reached'):
      mcp_server.main(
        [
          'mcp-server',
          'tasks',
          '--http',
          '--port',
          '0',
          '--port-file',
          str(port_file),
          '--bearer-token',
          't',
        ]
      )
    # --port 0 resolved to a real OS-assigned port
    assert 0 < int(port_file.read_text()) < 65536

  def test_http_bearer_token_can_come_from_environment(self, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setenv(mcp_server.BEARER_TOKEN_ENV, 'env-token')
    monkeypatch.setattr(mcp_server, '_resolve_servers', lambda spec: [])

    def capture_app(servers, token):
      captured['token'] = token
      raise RuntimeError('captured token')

    monkeypatch.setattr(mcp_server, 'create_http_app', capture_app)
    with pytest.raises(RuntimeError, match='captured token'):
      mcp_server.main(['mcp-server', 'tasks', '--http', '--port', '0'])
    assert captured['token'] == 'env-token'

  def test_http_requires_a_bearer_token_source(self, monkeypatch):
    monkeypatch.delenv(mcp_server.BEARER_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit, match='requires --port and --bearer-token or'):
      mcp_server.main(['mcp-server', 'tasks', '--http', '--port', '0'])

  def test_port_file_requires_http(self):
    with pytest.raises(SystemExit, match='only apply with --http'):
      mcp_server.main(['mcp-server', 'tasks', '--port-file', '/tmp/port'])


class TestHealth:
  def test_lists_namespaces_without_auth(self):
    with _client(_ShimBro()._live_mcp_servers()) as client:
      response = client.get('/health')
      assert response.status_code == 200
      assert response.json() == {'status': 'ok', 'namespaces': ['noop-source', 'ping']}


class TestBearerAuth:
  def test_missing_token_rejected(self):
    with _client([_create_ping_server()]) as client:
      response = client.post('/ping', json={})
      assert response.status_code == 401

  def test_wrong_token_rejected(self):
    with _client([_create_ping_server()]) as client:
      response = client.post('/ping', json={}, headers={'Authorization': 'Bearer wrong'})
      assert response.status_code == 401


class TestNamespaceEndpoints:
  def test_tools_keep_local_names(self):
    # the namespace reaches the client through the endpoint, so the listing
    # carries bare local names (`_ping`), not `ping___ping` wire names.
    with _client(_ShimBro()._live_mcp_servers()) as client:
      body = _rpc(client, '/ping', 'tools/list')
      assert [t['name'] for t in body['result']['tools']] == ['_ping']

  def test_data_source_served_on_own_endpoint(self):
    with _client(_ShimBro()._live_mcp_servers()) as client:
      body = _rpc(client, '/noop-source', 'tools/list')
      assert {t['name'] for t in body['result']['tools']} == {'search', 'fetch'}

  def test_multiple_searchable_sources_do_not_collide(self):
    # two searchable sources both expose bare `search` / `fetch`; each lives on
    # its own `<name>-source` endpoint in a raw bro session.
    with _client(_TwoSourceBro()._live_mcp_servers()) as client:
      for path in ('/noop-source', '/second-source'):
        body = _rpc(client, path, 'tools/list')
        assert {t['name'] for t in body['result']['tools']} == {'search', 'fetch'}

  def test_unknown_endpoint_404(self):
    with _client([_create_ping_server()]) as client:
      response = client.post('/nope', json={}, headers=_MCP_HEADERS)
      assert response.status_code == 404

  def test_tool_call(self):
    with _client([_create_ping_server()]) as client:
      body = _rpc(client, '/ping', 'tools/call', {'name': '_ping', 'arguments': {}})
      assert body['result']['content'] == [{'type': 'text', 'text': 'pong'}]

  def test_same_namespace_servers_merge(self):
    def _pong() -> str:
      return 'ping'

    describe(_pong, 'pong the noop server')
    servers = [_create_ping_server(), InProcessMCPServer('ping', [FunctionTool(_pong)])]
    with _client(servers) as client:
      body = _rpc(client, '/ping', 'tools/list')
      assert {t['name'] for t in body['result']['tools']} == {'_ping', '_pong'}

  def test_duplicate_tool_in_namespace_raises(self):
    with pytest.raises(SystemExit, match="duplicate tool name '_ping'"):
      create_http_app([_create_ping_server(), _create_ping_server()], TOKEN)


class TestCredsRendering:
  def test_description_renders_against_availability(self, monkeypatch):
    from bro.base import credentials

    monkeypatch.setattr(credentials, 'available', lambda name: False)
    with _client(_ShimBro()._live_mcp_servers()) as client:
      body = _rpc(client, '/noop-source', 'tools/list')
      fetch = next(t for t in body['result']['tools'] if t['name'] == 'fetch')
      # SearchableDataSource's fetch description carries a credential directive on the
      # summary LLM key; with the secret absent the listing advertises raw-only mode
      assert '{{' not in fetch['description']
      assert 'summarisation is unavailable' in fetch['description']
