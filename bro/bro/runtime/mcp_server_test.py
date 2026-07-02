from typing import ClassVar

import pytest
from starlette.testclient import TestClient

from bro.bro import BaseBro
from bro.datasources.searchable import Hit, SearchableDataSource
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, describe
from mcp_server import _resolve_servers, create_http_app

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


class _ShimBro(BaseBro):
  name = 'shim-test'
  description = 'composes a ping server and a data source'
  data_sources: ClassVar = [_NoopSource()]
  mcp_servers: ClassVar = [_create_ping_server]

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
  resp = client.post(path, json=payload, headers=headers if headers is not None else _MCP_HEADERS)
  assert resp.status_code == 200, resp.text
  return resp.json()


class TestResolveServers:
  def test_static_flow(self):
    servers = _resolve_servers('flow')
    assert len(servers) == 1
    assert servers[0].namespace == 'flow'

  def test_unknown(self):
    with pytest.raises(SystemExit, match='unknown server'):
      _resolve_servers('does-not-exist')

  def test_bro_spec_includes_service_namespace(self):
    namespaces = {s.namespace for s in _resolve_servers('bro:pm')}
    assert 'bro' in namespaces
    assert 'flow' in namespaces


class TestHealth:
  def test_lists_namespaces_without_auth(self):
    with _client(_ShimBro()._mcp_servers) as client:
      resp = client.get('/health')
      assert resp.status_code == 200
      assert resp.json() == {'status': 'ok', 'namespaces': ['noop-source', 'ping']}


class TestBearerAuth:
  def test_missing_token_rejected(self):
    with _client([_create_ping_server()]) as client:
      resp = client.post('/ping', json={})
      assert resp.status_code == 401

  def test_wrong_token_rejected(self):
    with _client([_create_ping_server()]) as client:
      resp = client.post('/ping', json={}, headers={'Authorization': 'Bearer wrong'})
      assert resp.status_code == 401


class TestNamespaceEndpoints:
  def test_tools_keep_local_names(self):
    # the namespace reaches the client through the endpoint, so the listing
    # carries bare local names (`_ping`), not `ping___ping` wire names.
    with _client(_ShimBro()._mcp_servers) as client:
      body = _rpc(client, '/ping', 'tools/list')
      assert [t['name'] for t in body['result']['tools']] == ['_ping']

  def test_data_source_served_on_own_endpoint(self):
    with _client(_ShimBro()._mcp_servers) as client:
      body = _rpc(client, '/noop-source', 'tools/list')
      assert {t['name'] for t in body['result']['tools']} == {'search', 'fetch'}

  def test_multiple_searchable_sources_do_not_collide(self):
    # two searchable sources both expose bare `search` / `fetch`; each lives on
    # its own `<name>-source` endpoint (the `cw ss --bro librorian` case).
    with _client(_TwoSourceBro()._mcp_servers) as client:
      for path in ('/noop-source', '/second-source'):
        body = _rpc(client, path, 'tools/list')
        assert {t['name'] for t in body['result']['tools']} == {'search', 'fetch'}

  def test_unknown_endpoint_404(self):
    with _client([_create_ping_server()]) as client:
      resp = client.post('/nope', json={}, headers=_MCP_HEADERS)
      assert resp.status_code == 404

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


class TestHasCredRendering:
  def test_description_renders_against_availability(self, monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server.credentials, 'available', lambda name: False)
    with _client(_ShimBro()._mcp_servers) as client:
      body = _rpc(client, '/noop-source', 'tools/list')
      fetch = next(t for t in body['result']['tools'] if t['name'] == 'fetch')
      # SearchableDataSource's fetch description carries a has_cred block on the
      # summary LLM key; with the secret absent the listing advertises raw-only mode
      assert '{{' not in fetch['description']
      assert 'summarisation is unavailable' in fetch['description']
