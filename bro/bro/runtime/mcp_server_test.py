import pytest

from bro.bro import BaseBro
from bro.datasources.base import Hit, SearchableDataSource
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, describe
from mcp_server import _Aggregate, _resolve_server


class _NoopSource(SearchableDataSource):
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
  return InProcessMCPServer('ping', [FunctionTool(_ping)])


class _ShimBro(BaseBro):
  name = 'shim-test'
  description = 'composes a ping server and a data source'
  data_sources = [_NoopSource()]
  mcp_servers = [_create_ping_server]

  def __init__(self):
    super().__init__(system_prompt='test')


class _CollidingBro(BaseBro):
  name = 'colliding'
  description = 'two servers that both expose ping'
  mcp_servers = [_create_ping_server, _create_ping_server]

  def __init__(self):
    super().__init__(system_prompt='test')


class _SecondSource(SearchableDataSource):
  name = 'second'
  summary = 'another no-op data source'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def fetch(self, id: str, query: str | None = None) -> str:
    return ''


class _TwoSourceBro(BaseBro):
  name = 'two-source'
  description = 'two searchable sources that both expose search/fetch'
  data_sources = [_NoopSource(), _SecondSource()]

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
    # `_Aggregate` advertises namespaced wire names: the ping server's namespace
    # is `ping`, the data source's is `noop-source`.
    assert 'ping___ping' in names
    assert 'noop-source__search' in names
    assert 'noop-source__fetch' in names

  @pytest.mark.asyncio
  async def test_duplicate_tool_raises(self):
    bro = _CollidingBro()
    server = _Aggregate('colliding', bro._mcp_servers)
    with pytest.raises(SystemExit, match="duplicate tool wire name 'ping___ping'"):
      await server.list_tools()

  @pytest.mark.asyncio
  async def test_multiple_searchable_sources_do_not_collide(self):
    # two searchable sources both expose bare `search` / `fetch`; without the
    # namespace they would collide here (the `cw ss --bro librorian` case).
    bro = _TwoSourceBro()
    server = _Aggregate('two-source', bro._mcp_servers)
    names = {t.name for t in await server.list_tools()}
    assert names == {
      'noop-source__search',
      'noop-source__fetch',
      'second-source__search',
      'second-source__fetch',
    }
