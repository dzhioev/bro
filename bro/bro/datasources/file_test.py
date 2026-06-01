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


@pytest.mark.asyncio
async def test_as_mcp_server_exposes_single_read_tool(env_file):
  server = FileSource('environment', summary='x', path=env_file).as_mcp_server()
  tools = await server.list_tools()
  assert [t.name for t in tools] == ['environment-read']


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
