import pytest

from bro.base.text_window import BYTE_LIMIT
from bro.datasources import references
from bro.datasources.file import FileSource
from bro.datasources.man import ManSource


@pytest.fixture
def source(tmp_path):
  first = tmp_path / 'first.md'
  first.write_text('# First\n\nfirst body\n')
  second = tmp_path / 'second.md'
  second.write_text('# Second\n\nsecond body\n')
  return ManSource(
    'man',
    summary='the reference pages',
    pages=[
      FileSource('first', summary='the first page', path=first),
      FileSource('second-page', summary='the second page', path=second),
    ],
  )


def test_read_returns_the_page_body(source):
  assert source.read('first') == '# First\n\nfirst body\n'


def test_read_tolerates_case_and_whitespace(source):
  assert source.read(' Second-Page ') == '# Second\n\nsecond body\n'


def test_read_names_the_topics_on_a_miss(source):
  with pytest.raises(LookupError, match='first, second-page'):
    source.read('third')


def test_declaring_no_pages_raises():
  with pytest.raises(ValueError, match='no pages'):
    ManSource('man', summary='x', pages=[])


def test_reads_a_long_page_whole(tmp_path):
  page = tmp_path / 'long.md'
  body = '\n'.join(f'line {index} ' + 'x' * 100 for index in range(BYTE_LIMIT // 50))
  page.write_text(body)
  source = ManSource('man', summary='x', pages=[FileSource('long', summary='x', path=page)])
  assert source.read('long') == body


@pytest.mark.asyncio
async def test_read_tool_serves_the_roster(source):
  server = source.as_mcp_server()
  assert server.namespace == 'man-source'
  tools = await server.list_tools()
  assert [tool.name for tool in tools] == ['read']
  # the roster must reach a surface that sees only the tool listing
  assert 'the first page' in tools[0].description
  assert 'second-page' in tools[0].description
  assert tools[0].parameters['properties']['topic']['enum'] == ['first', 'second-page']


@pytest.mark.asyncio
async def test_read_tool_returns_the_page(source):
  tool = (await source.as_mcp_server().list_tools())[0]
  assert await tool.call({'topic': 'first'}) == '# First\n\nfirst body\n'


def test_page_resolves_a_topic_of_the_repo_roster():
  page = references.page('dive-in')
  assert page.name == 'dive-in'
  assert len(page.read()) > 0


def test_page_names_the_topics_on_an_unknown_one():
  with pytest.raises(LookupError, match='dive-in'):
    references.page('no-such-topic')
