import pytest

from bro.bro import Bro, McpServerSpec
from bro.datasources.base import DataSource, Hit
from bro.show import format_card
from llm.mcp import FunctionTool, InProcessMCPServer, describe


def _make_server(*tool_names: str) -> InProcessMCPServer:
  tools = []
  for name in tool_names:

    def fn() -> str:
      return 'ok'

    fn.__name__ = name
    describe(fn, f'{name} tool description')
    tools.append(FunctionTool(fn))
  return InProcessMCPServer(tools)


def make_server_ab():
  return _make_server('a', 'b')


def make_server_xyz():
  return _make_server('x', 'y', 'z')


class _StubSource(DataSource):
  name = 'stub'
  summary = 'a stub data source for tests'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def fetch(self, id: str, query: str | None = None) -> str:
    return ''


class _MinimalBro(Bro):
  name = 'minimal'
  description = 'has nothing extra'

  def __init__(self):
    super().__init__(system_prompt='you are minimal')


class _FullBro(Bro):
  name = 'full'
  description = 'has a data source and two MCP servers'
  reasoning_effort = 'medium'
  data_sources = [_StubSource()]
  mcp_servers = [
    make_server_ab,
    McpServerSpec(make_server_xyz, allowed_tools=['x', 'z']),
  ]

  def __init__(self):
    super().__init__(system_prompt='YOU ARE FULL')


class TestFormatCard:
  @pytest.mark.asyncio
  async def test_header_includes_name_and_description(self):
    card = await format_card(_MinimalBro())
    assert card.startswith('# minimal\n')
    assert 'has nothing extra' in card

  @pytest.mark.asyncio
  async def test_identity_includes_model(self):
    card = await format_card(_MinimalBro())
    assert '- model: `' in card

  @pytest.mark.asyncio
  async def test_identity_omits_reasoning_effort_when_none(self):
    card = await format_card(_MinimalBro())
    assert 'reasoning effort' not in card

  @pytest.mark.asyncio
  async def test_identity_includes_reasoning_effort_when_set(self):
    card = await format_card(_FullBro())
    assert '- reasoning effort: `medium`' in card

  @pytest.mark.asyncio
  async def test_data_sources_section_omitted_when_empty(self):
    card = await format_card(_MinimalBro())
    assert '## Data sources' not in card

  @pytest.mark.asyncio
  async def test_data_sources_section_renders_each_source(self):
    card = await format_card(_FullBro())
    assert '## Data sources' in card
    assert '- **stub** — a stub data source for tests' in card

  @pytest.mark.asyncio
  async def test_mcp_section_omitted_when_empty(self):
    card = await format_card(_MinimalBro())
    assert '## MCP servers' not in card

  @pytest.mark.asyncio
  async def test_mcp_section_renders_factories_and_tools(self):
    card = await format_card(_FullBro())
    assert '## MCP servers' in card
    assert '`bro.show_test.make_server_ab` — 2 tools' in card
    assert '  - `a` — a tool description' in card
    assert '  - `b` — b tool description' in card

  @pytest.mark.asyncio
  async def test_mcp_filtered_badge_and_filtered_tool_list(self):
    card = await format_card(_FullBro())
    assert '`bro.show_test.make_server_xyz` — 2 tools (filtered)' in card
    assert '  - `x` — x tool description' in card
    assert '  - `z` — z tool description' in card
    assert '`y`' not in card

  @pytest.mark.asyncio
  async def test_system_prompt_omitted_by_default(self):
    card = await format_card(_FullBro())
    assert '## System prompt' not in card
    assert 'YOU ARE FULL' not in card

  @pytest.mark.asyncio
  async def test_system_prompt_included_when_requested(self):
    card = await format_card(_FullBro(), include_system_prompt=True)
    assert '## System prompt' in card
    assert 'YOU ARE FULL' in card
    assert '```' in card

  @pytest.mark.asyncio
  async def test_card_ends_with_newline(self):
    card = await format_card(_MinimalBro())
    assert card.endswith('\n')

  @pytest.mark.asyncio
  async def test_long_tool_description_truncated(self):
    long_desc = 'x' * 300

    def long_fn() -> str:
      return 'ok'

    long_fn.__name__ = 'longy'
    describe(long_fn, long_desc)

    def factory():
      return InProcessMCPServer([FunctionTool(long_fn)])

    class _LongBro(Bro):
      name = 'long'
      description = 'd'
      mcp_servers = [factory]

      def __init__(self):
        super().__init__(system_prompt='')

    card = await format_card(_LongBro())
    assert '…' in card
    assert long_desc not in card
