import re

import pytest

from bro.datasources.current_time import CurrentTime


def test_get_time_format():
  result = CurrentTime().get_time()
  assert re.fullmatch(
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} \([A-Za-z_]+/[A-Za-z_]+\)', result
  )


@pytest.mark.asyncio
async def test_as_mcp_server_exposes_single_get_time_tool():
  server = CurrentTime().as_mcp_server()
  tools = await server.list_tools()
  assert [t.name for t in tools] == ['current-time-get-time']


@pytest.mark.asyncio
async def test_get_time_tool_returns_current_time():
  server = CurrentTime().as_mcp_server()
  tool = (await server.list_tools())[0]
  result = await tool.call({})
  assert isinstance(result, str)
  assert re.fullmatch(
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} \([A-Za-z_]+/[A-Za-z_]+\)', result
  )
