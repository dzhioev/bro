import pytest

from bro.bro import Bro
from bro.datasources.base import DataSource, Hit
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, describe
from mcp_server import _Aggregate, _resolve_server


class _NoopSource(DataSource):
  name = 'noop'
  summary = 'no-op data source'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def fetch(self, id: str, query: str | None = None) -> str:
    return ''


def _ping() -> str:
  return 'pong'


describe(_ping, 'ping the noop server')


def _create_ping_server() -> MCPServer:
  return InProcessMCPServer([FunctionTool(_ping)])


class _ShimBro(Bro):
  name = 'shim-test'
  description = 'composes a ping server and a data source'
  data_sources = [_NoopSource()]
  mcp_servers = [_create_ping_server]

  def __init__(self):
    super().__init__(system_prompt='test')


class _CollidingBro(Bro):
  name = 'colliding'
  description = 'two servers that both expose ping'
  mcp_servers = [_create_ping_server, _create_ping_server]

  def __init__(self):
    super().__init__(system_prompt='test')


class TestResolveServer:
  def test_static_flow(self):
    server = _resolve_server('flow')
    assert isinstance(server, MCPServer)

  def test_unknown(self):
    with pytest.raises(SystemExit, match='unknown server'):
      _resolve_server('does-not-exist')


class TestAggregate:
  @pytest.mark.asyncio
  async def test_union_of_tools(self):
    server = _Aggregate('test', [_ShimBro()._mcp_servers[0], _ShimBro()._mcp_servers[1]])
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert '_ping' in names
    assert 'noop-search' in names
    assert 'noop-fetch' in names

  @pytest.mark.asyncio
  async def test_duplicate_tool_raises(self):
    bro = _CollidingBro()
    server = _Aggregate('colliding', bro._mcp_servers)
    with pytest.raises(SystemExit, match="duplicate tool name '_ping'"):
      await server.list_tools()
