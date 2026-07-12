import pytest

from bro.datasources.file import FileSource


@pytest.fixture
def env_file(tmp_path):
  p = tmp_path / 'environment.md'
  p.write_text('# Environment\n\nbody text\n')
  return p


def test_name_and_summary_are_attributes(env_file):
  src = FileSource('environment', summary='session playbook', path=env_file)
  assert src.name == 'environment'
  assert src.summary == 'session playbook'


def test_read_returns_file_body(env_file):
  src = FileSource('environment', summary='session playbook', path=env_file)
  assert src.read() == '# Environment\n\nbody text\n'


def test_read_picks_up_file_edits(env_file):
  src = FileSource('environment', summary='session playbook', path=env_file)
  assert 'body text' in src.read()
  env_file.write_text('replaced')
  # not cached — the source reads fresh on each call
  assert src.read() == 'replaced'


def test_read_renders_directives_for_the_bro_harness(env_file):
  env_file.write_text('{{iff #harness = bro}}call the tool{{else}}run the CLI{{end}}')
  src = FileSource('environment', summary='x', path=env_file)
  assert src.read() == 'call the tool'


def test_read_verbatim_serves_directive_payload_raw(env_file):
  # a doc about the directive syntax carries examples that rendering would
  # execute (or crash on — #wire is not a fact FileSource supplies)
  body = 'grammar: {{iff #harness = bro}}…{{end}} and {{assert #wire = bare}}'
  env_file.write_text(body)
  src = FileSource('conditions', summary='x', path=env_file, render=False)
  assert src.read() == body


@pytest.mark.asyncio
async def test_as_mcp_server_exposes_single_read_tool(env_file):
  server = FileSource('environment', summary='x', path=env_file).as_mcp_server()
  assert server.namespace == 'environment-source'
  tools = await server.list_tools()
  assert [t.name for t in tools] == ['read']


@pytest.mark.asyncio
async def test_read_tool_returns_file_body(env_file):
  server = FileSource('environment', summary='x', path=env_file).as_mcp_server()
  tool = (await server.list_tools())[0]
  assert await tool.call({}) == '# Environment\n\nbody text\n'


@pytest.mark.asyncio
async def test_read_tool_description_mentions_source_name(env_file):
  server = FileSource('environment', summary='x', path=env_file).as_mcp_server()
  tool = (await server.list_tools())[0]
  assert 'environment' in tool.description
