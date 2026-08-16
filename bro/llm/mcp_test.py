import asyncio
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Optional

import pytest
from pydantic import Field, ValidationError

from bro.base.condition import ConditionError, SetVariable, when
from bro.llm import mcp as mcp_mod
from bro.llm.mcp import (
  FunctionTool,
  InProcessMCPServer,
  ToolRegistry,
  UnknownToolError,
  canonical_name,
  describe,
  namespaced_tools,
  render_return_shape,
  render_text,
  select,
  validated_callable,
  wire_name,
)


class _Color(Enum):
  RED = 'red'
  BLUE = 'blue'


@dataclass
class _Item:
  id: str
  tags: list[str]
  color: _Color
  note: Optional[str]


class TestReturnShape:
  def _schema(self, function) -> dict:
    describe(function, 'd')
    schema = FunctionTool(function).output_schema
    assert schema is not None
    return schema

  def test_object_with_enum_list_and_optional(self):
    def make() -> _Item:
      raise NotImplementedError

    assert render_return_shape(self._schema(make)) == (
      '{\n  id: str,\n  tags: str[],\n  color: "red"|"blue",\n  note: str|null\n}'
    )

  def test_list_return_unwraps_and_names_element(self):
    def make() -> list[_Item]:
      raise NotImplementedError

    assert render_return_shape(self._schema(make)).startswith('_Item{')
    assert render_return_shape(self._schema(make)).endswith('}[]')

  def test_optional_return_unwraps_to_nullable(self):
    def make() -> Optional[_Item]:
      raise NotImplementedError

    assert render_return_shape(self._schema(make)).endswith('}|null')

  def test_str_return_omits_shape_from_description(self):
    def echo(x: Annotated[str, Field(description='x')]) -> str:
      return x

    describe(echo, 'echo')
    assert FunctionTool(echo).description == 'echo'

  def test_structured_return_appends_shape_to_description(self):
    def make() -> _Item:
      raise NotImplementedError

    describe(make, 'make an item')
    description = FunctionTool(make).description
    assert description.startswith('make an item\n\nReturns: {')

  def test_shapeless_dict_return_omits_the_returns_line(self):
    from typing import Any

    def status() -> dict[str, Any]:
      return {}

    describe(status, 'returns `{state, answer}`')
    tool = FunctionTool(status)
    assert tool.description == 'returns `{state, answer}`'
    assert tool.output_schema is not None  # only the description line is dropped


@dataclass
class _Pair:
  x: int
  label: str


class TestOutputValidation:
  def _tool(self, function) -> FunctionTool:
    describe(function, 'd')
    return FunctionTool(function)

  def test_validate_output_accepts_conforming(self):
    def make() -> _Pair:
      return _Pair(x=1, label='a')

    self._tool(make).validate_output(_Pair(x=1, label='a'))

  def test_validate_output_raises_on_wrong_type(self):
    def make() -> _Pair:
      return _Pair(x=1, label='a')

    with pytest.raises(ValidationError):
      self._tool(make).validate_output({'x': 'not-an-int', 'label': 'a'})

  def test_validate_output_raises_on_missing_field(self):
    def make() -> _Pair:
      return _Pair(x=1, label='a')

    with pytest.raises(ValidationError):
      self._tool(make).validate_output({'x': 1})

  def test_validate_output_raises_on_wrong_list_shape(self):
    def make() -> list[_Pair]:
      return [_Pair(x=1, label='a')]

    with pytest.raises(ValidationError):
      self._tool(make).validate_output('not-a-list')

  def test_validate_output_str_tool_rejects_non_str(self):
    def echo(x: Annotated[str, Field(description='x')]) -> str:
      return x

    tool = self._tool(echo)
    tool.validate_output('fine')
    with pytest.raises(AssertionError):
      tool.validate_output(123)

  @pytest.mark.asyncio
  async def test_validated_callable_returns_result_unchanged(self):
    def make(n: Annotated[int, Field(description='n')]) -> _Pair:
      return _Pair(x=n, label='a')

    wrapped = validated_callable(self._tool(make))
    assert await wrapped(n=5) == _Pair(x=5, label='a')

  @pytest.mark.asyncio
  async def test_validated_callable_raises_on_drift(self):
    def make() -> _Pair:
      return {'x': 1}  # type: ignore[return-value]  # simulate backend drift

    wrapped = validated_callable(self._tool(make))
    with pytest.raises(ValidationError):
      await wrapped()


class TestFunctionTool:
  def test_sync_function(self):
    def greet(name: Annotated[str, Field(description='person name')]) -> str:
      return f'hello {name}'

    describe(greet, 'say hello')

    tool = FunctionTool(greet)
    assert tool.name == 'greet'
    assert tool.description == 'say hello'
    assert 'name' in tool.parameters.get('properties', {})

  def test_custom_name_and_description(self):
    def greet(name: str) -> str:
      return f'hello {name}'

    describe(greet, 'original description')

    tool = FunctionTool(greet, name='say_hi', description='custom desc')
    assert tool.name == 'say_hi'
    assert tool.description == 'custom desc'

  def test_no_description_raises(self):
    def no_doc(x: str) -> str:
      return x

    with pytest.raises(ValueError, match='no description'):
      FunctionTool(no_doc)

  @pytest.mark.asyncio
  async def test_call_sync(self):
    def add(
      a: Annotated[int, Field(description='first')],
      b: Annotated[int, Field(description='second')],
    ) -> str:
      return str(a + b)

    describe(add, 'add two numbers')

    tool = FunctionTool(add)
    result = await tool.call({'a': 3, 'b': 4})
    assert result == '7'

  @pytest.mark.asyncio
  async def test_call_async(self):
    async def upper(text: Annotated[str, Field(description='input text')]) -> str:
      return text.upper()

    describe(upper, 'uppercase text')

    tool = FunctionTool(upper)
    result = await tool.call({'text': 'hello'})
    assert result == 'HELLO'

  def test_optional_parameter(self):
    def search(
      query: Annotated[str, Field(description='search query')],
      limit: Annotated[Optional[int], Field(description='max results')] = None,
    ) -> str:
      return f'{query}:{limit}'

    describe(search, 'search for items')

    tool = FunctionTool(search)
    props = tool.parameters.get('properties', {})
    assert 'query' in props
    assert 'limit' in props

  @pytest.mark.asyncio
  async def test_call_with_optional_default(self):
    def greet(
      name: Annotated[str, Field(description='name')],
      greeting: Annotated[str, Field(description='greeting')] = 'hello',
    ) -> str:
      return f'{greeting} {name}'

    describe(greet, 'greet someone')

    tool = FunctionTool(greet)
    result = await tool.call({'name': 'world'})
    assert result == 'hello world'

    result = await tool.call({'name': 'world', 'greeting': 'hi'})
    assert result == 'hi world'

  def test_str_return_has_no_output_schema(self):
    def echo(text: Annotated[str, Field(description='text')]) -> str:
      return text

    describe(echo, 'echo')

    tool = FunctionTool(echo)
    assert tool.output_schema is None

  @pytest.mark.asyncio
  async def test_dataclass_return_produces_structured_output(self):
    @dataclass
    class Point:
      x: int
      y: int
      label: Optional[str]

    def make_point(
      x: Annotated[int, Field(description='x')],
      y: Annotated[int, Field(description='y')],
    ) -> Point:
      return Point(x=x, y=y, label=None)

    describe(make_point, 'make a point')

    tool = FunctionTool(make_point)
    schema = tool.output_schema
    assert schema is not None
    assert set(schema['properties'].keys()) == {'x', 'y', 'label'}

    result = await tool.call({'x': 1, 'y': 2})
    assert result == {'x': 1, 'y': 2, 'label': None}


class TestInProcessMCPServer:
  @pytest.mark.asyncio
  async def test_list_tools(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      return str(y)

    describe(tool_b, 'tool b')

    server = InProcessMCPServer('test', [FunctionTool(tool_a), FunctionTool(tool_b)])
    tools = await server.list_tools()
    assert len(tools) == 2
    # the server's own list_tools returns the local (in-namespace) names; the
    # namespacing happens at the assembling layer (ToolRegistry).
    assert tools[0].name == 'tool_a'
    assert tools[1].name == 'tool_b'

  @pytest.mark.asyncio
  async def test_empty_server(self):
    server = InProcessMCPServer('test', [])
    tools = await server.list_tools()
    assert tools == []

  @pytest.mark.asyncio
  async def test_rejects_double_underscore_in_namespace(self):
    with pytest.raises(ValueError, match='double underscore'):
      InProcessMCPServer('bad__ns', [])

  @pytest.mark.asyncio
  async def test_rejects_empty_namespace(self):
    with pytest.raises(ValueError, match='non-empty'):
      InProcessMCPServer('', [])

  @pytest.mark.asyncio
  async def test_rejects_double_underscore_in_tool_name(self):
    def bad(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(bad, 'bad tool')

    with pytest.raises(ValueError, match='double underscore'):
      InProcessMCPServer('test', [FunctionTool(bad, name='bad__tool')])

  @pytest.mark.asyncio
  async def test_list_tools_returns_copy(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    server = InProcessMCPServer('test', [FunctionTool(tool_a)])
    tools1 = await server.list_tools()
    tools2 = await server.list_tools()
    assert tools1 is not tools2
    assert tools1[0] is tools2[0]


class TestToolRegistry:
  @pytest.mark.asyncio
  async def test_resolve_from_single_server(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    registry = ToolRegistry([InProcessMCPServer('a', [FunctionTool(tool_a)])])
    tools = await registry.resolve()
    assert len(tools) == 1
    # the registry advertises namespaced wire names to the LLM.
    assert tools[0].name == 'a__tool_a'

  @pytest.mark.asyncio
  async def test_resolve_from_multiple_servers(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    def tool_b(y: Annotated[int, Field(description='number')]) -> str:
      return str(y)

    describe(tool_b, 'tool b')

    server_a = InProcessMCPServer('a', [FunctionTool(tool_a)])
    server_b = InProcessMCPServer('b', [FunctionTool(tool_b)])
    registry = ToolRegistry([server_a, server_b])
    tools = await registry.resolve()
    assert len(tools) == 2
    names = {t.name for t in tools}
    assert names == {'a__tool_a', 'b__tool_b'}

  @pytest.mark.asyncio
  async def test_same_local_name_distinct_across_namespaces(self):
    # the collision case namespaces exist to solve: two sources both exposing a
    # `search` tool stay distinct because the wire name carries the namespace.
    def search(query: Annotated[str, Field(description='q')]) -> str:
      return query

    describe(search, 'search')

    server_a = InProcessMCPServer('wikipedia-source', [FunctionTool(search)])
    server_b = InProcessMCPServer('tmdb-source', [FunctionTool(search)])
    registry = ToolRegistry([server_a, server_b])
    names = {t.name for t in await registry.resolve()}
    assert names == {'wikipedia-source__search', 'tmdb-source__search'}

  @pytest.mark.asyncio
  async def test_resolve_caches(self):
    def tool_a(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(tool_a, 'tool a')

    registry = ToolRegistry([InProcessMCPServer('a', [FunctionTool(tool_a)])])
    tools1 = await registry.resolve()
    tools2 = await registry.resolve()
    assert tools1[0] is tools2[0]

  @pytest.mark.asyncio
  async def test_duplicate_name_raises(self):
    def dupe(x: Annotated[str, Field(description='input')]) -> str:
      return x

    describe(dupe, 'duplicate')

    server_a = InProcessMCPServer('a', [FunctionTool(dupe)])
    server_b = InProcessMCPServer('a', [FunctionTool(dupe)])
    registry = ToolRegistry([server_a, server_b])
    with pytest.raises(ValueError, match="duplicate tool wire name.*namespace 'a'"):
      await registry.resolve()

  @pytest.mark.asyncio
  async def test_call_by_name(self):
    def reverse(text: Annotated[str, Field(description='text')]) -> str:
      return text[::-1]

    describe(reverse, 'reverse text')

    registry = ToolRegistry([InProcessMCPServer('a', [FunctionTool(reverse)])])
    result = await registry.call('a__reverse', {'text': 'hello'})
    assert result == 'olleh'

  @pytest.mark.asyncio
  async def test_call_unknown_raises(self):
    registry = ToolRegistry([InProcessMCPServer('test', [])])
    with pytest.raises(
      UnknownToolError, match="unknown or disallowed tool: 'nonexistent'"
    ) as exception:
      await registry.call('nonexistent', {})
    assert exception.value.name == 'nonexistent'

  @pytest.mark.asyncio
  async def test_empty_registry(self):
    registry = ToolRegistry([])
    tools = await registry.resolve()
    assert tools == []


class TestRenderText:
  def test_harness_branches(self):
    text = 'watch: {{iff #harness = bro}}job/watch{{eliff #harness = claude}}Monitor{{end}}'
    assert render_text(text, harness='bro') == 'watch: job/watch'
    assert render_text(text, harness='claude') == 'watch: Monitor'

  def test_wire_branches(self):
    text = 'call {{iff #wire = bare}}ns__tool{{eliff #wire = mcp}}mcp__ns__tool{{end}}'
    assert render_text(text, wire='bare') == 'call ns__tool'
    assert render_text(text, wire='mcp') == 'call mcp__ns__tool'

  def test_creds_membership_probes_availability(self, monkeypatch):
    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: name == 'openai')
    text = '{{iff #creds contains openai}}summarized{{else}}raw{{end}}'
    assert render_text(text, creds=['openai']) == 'summarized'
    text = '{{iff #creds contains github}}push{{else}}no push{{end}}'
    assert render_text(text, creds=['openai', 'github']) == 'no push'

  def test_creds_outside_universe_raises(self, monkeypatch):
    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: True)
    with pytest.raises(ValueError, match='universe'):
      render_text('{{iff #creds contains typo}}x{{else}}y{{end}}', creds=['openai'])

  def test_creds_probed_lazily(self, monkeypatch):
    # only the tested name resolves — a large universe costs nothing extra.
    probed: list[str] = []

    def available(name: str) -> bool:
      probed.append(name)
      return True

    monkeypatch.setattr(mcp_mod.credentials, 'available', available)
    render_text('{{when #creds contains openai}}x{{end}}', creds=['openai', 'github', 'notion'])
    assert probed == ['openai']

  def test_absent_fact_raises_on_reference(self):
    with pytest.raises(ValueError, match='unknown variable #wire'):
      render_text('{{when #wire = bare}}x{{end}}', harness='bro')

  def test_facts_combine(self):
    text = '{{when #harness = bro}}B{{end}}{{when #wire = mcp}}M{{end}}'
    assert render_text(text, harness='bro', wire='mcp') == 'BM'

  def test_plain_text_unchanged_without_consulting_availability(self, monkeypatch):
    def boom(name: str) -> bool:
      raise AssertionError('availability must not be consulted with no directive')

    monkeypatch.setattr(mcp_mod.credentials, 'available', boom)
    assert render_text('plain text', creds=[]) == 'plain text'

  def test_unknown_literal_raises(self):
    with pytest.raises(ValueError, match='domain'):
      render_text('{{when #harness = claud}}x{{end}}', harness='claude')

  def test_unknown_harness_argument_raises(self):
    with pytest.raises(ValueError, match='unknown harness'):
      render_text('{{iff a = a}}x{{end}}', harness='gemini')  # type: ignore[arg-type]

  def test_unknown_wire_argument_raises(self):
    with pytest.raises(ValueError, match='unknown wire'):
      render_text('{{iff a = a}}x{{end}}', wire='grpc')  # type: ignore[arg-type]

  def test_hold_fact_selects_a_branch(self):
    text = '{{iff #hold = unattended}}U{{else}}other{{end}}'
    assert render_text(text, hold='unattended') == 'U'
    assert render_text(text, hold='guided') == 'other'

  def test_hold_undefined_outside_hold_text(self):
    # the hold fact is supplied only when rendering the hold text, so a
    # stray #hold directive in hold-neutral text fails instead of picking a side
    with pytest.raises(ValueError, match='unknown variable #hold'):
      render_text('{{when #hold = unattended}}x{{end}}', harness='claude', wire='mcp')

  def test_unknown_hold_argument_raises(self):
    with pytest.raises(ValueError, match='unknown hold'):
      render_text('{{iff a = a}}x{{end}}', hold='automatic')

  def test_include_resolves_through_prompts_loader(self, monkeypatch):
    from bro import prompts

    files = {'x.md': 'spliced {{iff #harness = bro}}B{{eliff #harness = claude}}C{{end}}'}
    monkeypatch.setattr(prompts, 'get_prompt', lambda name: files[name])
    assert render_text('root: {{include x.md}}', harness='bro') == 'root: spliced B'
    assert render_text('root: {{include x.md}}', harness='claude') == 'root: spliced C'

  def test_include_escaping_the_prompts_directory_raises(self):
    with pytest.raises(ValueError, match='escapes the prompts directory'):
      render_text('{{include ../CLAUDE.md}}', harness='bro')

  @pytest.mark.asyncio
  async def test_namespaced_tool_passes_rendered_description_through(self):
    # tool text leaves its server fully rendered; the assembling wrapper only
    # rewrites the name
    def fetch(id: Annotated[str, Field(description='id')]) -> str:
      return id

    describe(fetch, 'fetch a record; raw only')
    tools = await namespaced_tools(InProcessMCPServer('src', [FunctionTool(fetch)]))
    assert tools[0].name == 'src__fetch'
    assert tools[0].description == 'fetch a record; raw only'


class TestToolVariables:
  def _variables(self, *selected: str, universe: tuple[str, ...] = ()) -> dict:
    names = universe if len(universe) > 0 else selected
    return {'tools': SetVariable(frozenset(selected), universe=frozenset(names))}

  def test_description_renders_against_the_vocabulary(self):
    def helper(x: str) -> str:
      return x

    describe(helper, 'help{{when #tools contains sibling}}; see sibling{{end}}')
    with_sibling = self._variables('helper', 'sibling')
    without = self._variables('helper', universe=('helper', 'sibling'))
    assert FunctionTool(helper, variables=with_sibling).description == 'help; see sibling'
    assert FunctionTool(helper, variables=without).description == 'help'

  def test_parameter_annotations_render_too(self):
    def helper(
      x: Annotated[str, Field(description='x{{when #tools contains sibling}}; via sibling{{end}}')],
    ) -> str:
      return x

    describe(helper, 'help')
    tool = FunctionTool(helper, variables=self._variables('helper', 'sibling'))
    assert tool.parameters['properties']['x']['description'] == 'x; via sibling'

  def test_unknown_sibling_raises_at_construction(self):
    def helper(x: str) -> str:
      return x

    describe(helper, 'help{{when #tools contains sibilng}}; see it{{end}}')
    with pytest.raises(ValueError, match='outside the set universe'):
      FunctionTool(helper, variables=self._variables('helper', 'sibling'))

  def test_directive_without_vocabulary_raises_instead_of_leaking(self):
    def helper(x: str) -> str:
      return x

    describe(helper, 'help{{when #tools contains sibling}}; see sibling{{end}}')
    with pytest.raises(ValueError, match='unknown variable #tools'):
      FunctionTool(helper)


class TestToolLayer:
  @pytest.mark.parametrize(
    ('tool_names', 'error_type', 'message'),
    [
      ((), ValueError, 'must mount a server or block'),
      (('',), TypeError, 'non-empty strings'),
      (('Read', 'Read'), ValueError, 'duplicate names'),
    ],
  )
  def test_block_rejects_invalid_declarations(self, tool_names, error_type, message):
    with pytest.raises(error_type, match=message):
      mcp_mod.block(*tool_names)

  def test_mount_selects_from_one_toolset_type(self):
    toolset = mcp_mod.Toolset('layer')

    @toolset.tool('read')
    def read() -> str:
      return 'read'

    full = mcp_mod.mount(toolset)
    selected = mcp_mod.mount(toolset, 'read')

    assert isinstance(full, mcp_mod.ToolLayer)
    assert isinstance(selected, mcp_mod.ToolLayer)
    assert len(full.server_specs) == 1
    assert len(selected.server_specs) == 1


class TestToolsetRendering:
  def _toolset(self) -> mcp_mod.Toolset:
    toolset = mcp_mod.Toolset('pack')

    @toolset.tool('read stuff{{when #tools contains manual}}; rules in manual{{end}}')
    def read(x: str) -> str:
      return x

    @toolset.tool('the shared rules')
    def manual() -> str:
      return 'rules'

    return toolset

  @pytest.mark.asyncio
  async def test_full_build_keeps_the_cross_reference(self):
    server = self._toolset().build()
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools['read'].description == 'read stuff; rules in manual'
    assert server.tool_universe == ('read', 'manual')

  @pytest.mark.asyncio
  async def test_scoped_build_drops_the_cross_reference(self):
    server = self._toolset().build('read')
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools['read'].description == 'read stuff'
    assert server.tool_universe == ('read', 'manual')

  def test_reference_outside_the_roster_raises_at_build(self):
    toolset = mcp_mod.Toolset('pack')

    @toolset.tool('read stuff{{when #tools contains manaul}}; typo{{end}}')
    def read(x: str) -> str:
      return x

    with pytest.raises(ValueError, match='outside the set universe'):
      toolset.build()


class TestSelect:
  def test_harness_condition_filters_entries(self):
    entries = ['plain', when(mcp_mod.harness == 'bro', 'devtools')]
    assert select(entries, harness='bro') == ['plain', 'devtools']
    assert select(entries, harness='claude') == ['plain']

  def test_creds_fact_probes_availability(self, monkeypatch):
    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: name == 'openai')
    entries = [when(mcp_mod.creds.contains('openai'), 'summary')]
    assert select(entries, creds=['openai']) == ['summary']
    monkeypatch.setattr(mcp_mod.credentials, 'available', lambda name: False)
    assert select(entries, creds=['openai']) == []

  def test_absent_fact_raises_on_reference(self):
    with pytest.raises(ConditionError, match='unknown variable #wire'):
      select([when(mcp_mod.wire == 'bare', 'x')], harness='bro')

  def test_unknown_harness_argument_raises(self):
    with pytest.raises(ValueError, match='unknown harness'):
      select([], harness='gemini')  # type: ignore[arg-type]


class TestWireName:
  def test_joins_with_double_underscore(self):
    assert wire_name('tasks', 'get_task_info') == 'tasks__get_task_info'

  def test_local_hyphen_and_single_underscore_preserved(self):
    assert wire_name('wikipedia-source', 'get_time') == 'wikipedia-source__get_time'

  def test_spell_namespace_uses_the_ordinary_spelling(self):
    assert wire_name('spell', 'send-email') == 'spell__send-email'


class TestCanonicalName:
  def test_inverts_wire_name(self):
    assert canonical_name('tasks__get_task_info') == 'tasks::get_task_info'
    assert canonical_name('wikipedia-source__get_time') == 'wikipedia-source::get_time'

  def test_unnamespaced_name_passes_through(self):
    assert canonical_name('banner') == 'banner'


class TestWithoutMCPPackage:
  def test_layer_imports_without_the_mcp_package(self):
    # only FunctionTool may reach for the `mcp` package; simulate its absence in a
    # fresh subprocess.
    import subprocess
    import sys

    code = (
      "import sys; sys.modules['mcp'] = None; "
      'import bro.llm.llm, bro.llm.llms.openai, bro.llm.mcp, bro.prompts; '
      "bro.llm.mcp.render_text('plain'); "
      "print('ok')"
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


class TestSyncToolExecution:
  @pytest.mark.asyncio
  async def test_sync_tool_runs_off_the_loop(self):
    # the release only ever comes from the event loop, so a tool that ran inline
    # would deadlock here instead of returning — which is the property under test.
    release = threading.Event()
    thread_ids: list[int] = []

    def blocker() -> str:
      thread_ids.append(threading.get_ident())
      release.wait(5)
      return 'done'

    call = asyncio.create_task(FunctionTool(blocker, description='blocks').call({}))
    await asyncio.sleep(0.05)
    assert not call.done()
    release.set()

    assert await asyncio.wait_for(call, timeout=5) == 'done'
    assert thread_ids != [threading.get_ident()]

  @pytest.mark.asyncio
  async def test_async_tool_is_awaited_directly(self):
    async def ping() -> str:
      return f'{threading.get_ident()}'

    assert await FunctionTool(ping, description='pings').call({}) == str(threading.get_ident())


class TestServerTeardown:
  def test_toolset_close_releases_the_built_state(self):
    class _State:
      def __init__(self):
        self.closed = False

      def close(self) -> None:
        self.closed = True

    states: list[_State] = []

    def make_state() -> _State:
      states.append(_State())
      return states[-1]

    toolset = mcp_mod.Toolset('probe', state=make_state, close=_State.close)

    @toolset.tool('does nothing')
    def noop() -> str:
      return 'ok'

    server = toolset.build()
    assert [state.closed for state in states] == [False]
    server.close()
    assert [state.closed for state in states] == [True]

  def test_close_is_a_no_op_without_declared_teardown(self):
    InProcessMCPServer('probe', []).close()
