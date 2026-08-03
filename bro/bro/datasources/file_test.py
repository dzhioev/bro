import pytest

from bro.base.template import TemplateError
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


def test_read_raises_on_a_surface_directive(env_file):
  # one rendering is read by every harness, so the body must be surface-neutral;
  # a harness fork fails loudly instead of silently picking a branch
  env_file.write_text('{{iff #harness = bro}}call the tool{{else}}run the CLI{{end}}')
  src = FileSource('environment', summary='x', path=env_file)
  with pytest.raises(TemplateError, match='#harness'):
    src.read()


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
async def test_read_tool_description_carries_name_and_summary(env_file):
  # the summary must reach surfaces that see only the tool listing (a
  # cw-session has no `## Data sources` block)
  server = FileSource('environment', summary='session playbook', path=env_file).as_mcp_server()
  tool = (await server.list_tools())[0]
  assert 'environment' in tool.description
  assert 'session playbook' in tool.description


def test_reference_sources_resolve_and_read():
  # every ready-made instance must point at a live repo file; a moved or
  # renamed doc would otherwise surface only at tool-call time
  from bro.datasources import references

  sources = [value for value in vars(references).values() if isinstance(value, FileSource)]
  assert len(sources) >= 4
  for source in sources:
    assert len(source.read()) > 0
