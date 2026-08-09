from typing import ClassVar

import pytest

import bro.llm.llms.chat_gpt as llm_llms_chat_gpt
import bro.llm.llms.echo as llm_llms_echo
from bro.bro import BaseBro
from bro.datasources.searchable import Hit, SearchableDataSource
from bro.llm.mcp import FunctionTool, InProcessMCPServer, MCPServerSpec, creds, describe
from bro.show import format_card


def _make_tools(*tool_names: str) -> list[FunctionTool]:
  tools = []
  for name in tool_names:

    def function() -> str:
      return 'ok'

    function.__name__ = name
    describe(function, f'{name} tool description')
    tools.append(FunctionTool(function))
  return tools


class ServerAB(InProcessMCPServer):
  needed_secrets = ('notion',)

  def __init__(self):
    super().__init__('ab', _make_tools('a', 'b'))


class ServerXZ(InProcessMCPServer):
  def __init__(self):
    super().__init__('xz', _make_tools('x', 'z'))


class _StubSource(SearchableDataSource):
  name = 'stub'
  summary = 'a stub data source for tests'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return []

  async def _fetch_content(self, id: str) -> str:
    return ''


class _MinimalBro(BaseBro):
  name = 'minimal'
  description = 'has nothing extra'

  def __init__(self):
    super().__init__(system_prompt='you are minimal')


class _FullBro(BaseBro):
  name = 'full'
  description = 'has a data source and two MCP servers'
  llm_spec = llm_llms_chat_gpt.LLMSpec(reasoning_effort='medium')
  data_sources: ClassVar = [_StubSource()]
  mcp_servers: ClassVar = [MCPServerSpec.of(ServerAB), MCPServerSpec.of(ServerXZ)]

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
    assert '## MCP tools' not in card

  @pytest.mark.asyncio
  async def test_mcp_section_renders_namespace_and_tools(self):
    card = await format_card(_FullBro())
    assert '## MCP tools' in card
    assert '- `ab` — 2 tools' in card
    assert '  - `a` — a tool description' in card
    assert '  - `b` — b tool description' in card

  @pytest.mark.asyncio
  async def test_mcp_second_namespace_rendered(self):
    card = await format_card(_FullBro())
    assert '- `xz` — 2 tools' in card
    assert '  - `x` — x tool description' in card
    assert '  - `z` — z tool description' in card

  @pytest.mark.asyncio
  async def test_mcp_servers_sharing_a_namespace_grouped(self):
    class ServerAB2(InProcessMCPServer):
      def __init__(self):
        super().__init__('ab', _make_tools('c'))

    class _SharedBro(BaseBro):
      name = 'shared'
      description = 'two servers in one namespace'
      mcp_servers: ClassVar = [MCPServerSpec.of(ServerAB), MCPServerSpec.of(ServerAB2)]

      def __init__(self):
        super().__init__(system_prompt='')

    card = await format_card(_SharedBro())
    assert '- `ab` — 3 tools' in card
    assert '  - `c` — c tool description' in card

  @pytest.mark.asyncio
  async def test_features_section_omitted_when_none_declared(self):
    card = await format_card(_MinimalBro())
    assert '## Features' not in card

  @pytest.mark.asyncio
  async def test_features_section_shows_gates_and_state(self, monkeypatch):
    class _FeatureBro(BaseBro):
      name = 'featured'
      description = 'has a gated feature'
      features: ClassVar = {'tracker': creds.contains('trackerkey')}

      def __init__(self):
        super().__init__(system_prompt='')

    monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'trackerkey')
    card = await format_card(_FeatureBro())
    assert '## Features' in card
    assert '- **tracker** — gated on `#creds contains trackerkey`; on in this environment' in card

    monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
    card = await format_card(_FeatureBro())
    assert '- **tracker** — gated on `#creds contains trackerkey`; off in this environment' in card

  @pytest.mark.asyncio
  async def test_pinned_feature_shows_always_on(self):
    class _PinnedBro(BaseBro):
      name = 'pinned'
      description = 'pins its feature'
      features: ClassVar = {'tracker': True}

      def __init__(self):
        super().__init__(system_prompt='')

    card = await format_card(_PinnedBro())
    assert '- **tracker** — always on' in card

  @pytest.mark.asyncio
  async def test_disabled_feature_shows_disabled(self):
    class _DisabledBro(BaseBro):
      name = 'disabled'
      description = 'disables its feature'
      features: ClassVar = {'tracker': False}

      def __init__(self):
        super().__init__(system_prompt='')

    card = await format_card(_DisabledBro())
    assert '- **tracker** — disabled' in card

  @pytest.mark.asyncio
  async def test_secrets_section_lists_manifest(self):
    # ServerAB declares `notion`; needed_secrets() is the component manifest (no llm)
    card = await format_card(_FullBro())
    assert '## Secrets' in card
    assert '- `notion`' in card

  @pytest.mark.asyncio
  async def test_secrets_section_lists_optional(self):
    # the stub source is a SearchableDataSource → openai is its optional summary key
    card = await format_card(_FullBro())
    assert '- `openai` — optional (used if present)' in card

  @pytest.mark.asyncio
  async def test_secrets_section_lists_llm_key(self):
    # the LLM key (chat_gpt → openai) surfaces on its own line beyond the manifest
    card = await format_card(_FullBro())
    assert '- `openai` — LLM key' in card

  @pytest.mark.asyncio
  async def test_secrets_section_notes_per_surface_baselines(self):
    card = await format_card(_FullBro())
    assert 'added per-surface' in card

  @pytest.mark.asyncio
  async def test_secrets_section_omitted_when_empty(self):

    class _KeylessBro(BaseBro):
      name = 'keyless'
      description = 'no secrets'
      llm_spec = llm_llms_echo.LLMSpec()

      def __init__(self):
        super().__init__(system_prompt='hi')

    card = await format_card(_KeylessBro())
    assert '## Secrets' not in card

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
  async def test_scripts_section_renders_canonical_roster_and_optional_secret(self, tmp_path):
    script_path = tmp_path / 'do-work.md'
    script_path.write_text('---\ndescription: develop the named task\n---\n\nbody')

    class _ScriptedBro(_MinimalBro):
      @property
      def scripts(self):
        return {'do-work': script_path}

    card = await format_card(_ScriptedBro())
    assert '## Scripts' in card
    assert '- **@::do-work** — develop the named task' in card
    assert '- `openai` — optional (used if present)' in card

  @pytest.mark.asyncio
  async def test_scripts_section_omitted_when_empty(self):
    card = await format_card(_MinimalBro())
    assert '## Scripts' not in card

  @pytest.mark.asyncio
  async def test_scripts_long_description_truncated(self, tmp_path):
    long_description = 'x' * 300
    script_path = tmp_path / 'foo.md'
    script_path.write_text(f'---\ndescription: {long_description}\n---\n\nbody')

    class _LongScriptBro(_MinimalBro):
      @property
      def scripts(self):
        return {'foo': script_path}

    card = await format_card(_LongScriptBro())
    assert '…' in card
    assert long_description not in card

  @pytest.mark.asyncio
  async def test_long_tool_description_truncated(self):
    long_description = 'x' * 300

    def long_function() -> str:
      return 'ok'

    long_function.__name__ = 'longy'
    describe(long_function, long_description)

    class LongServer(InProcessMCPServer):
      def __init__(self):
        super().__init__('long', [FunctionTool(long_function)])

    class _LongBro(BaseBro):
      name = 'long'
      description = 'd'
      mcp_servers: ClassVar = [MCPServerSpec.of(LongServer)]

      def __init__(self):
        super().__init__(system_prompt='')

    card = await format_card(_LongBro())
    assert '…' in card
    assert long_description not in card
