import re

import pytest

from bro.datasources import references
from bro.datasources.file import FileSource
from bro.datasources.man import PAGE_LIMIT, ManPage, ManSource, manual


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


def test_long_page_is_capped_and_resumed_at_the_offset_it_names(tmp_path):
  page = tmp_path / 'long.md'
  body = '\n'.join(f'line {index}' for index in range(PAGE_LIMIT * 2))
  page.write_text(body)
  source = ManSource('man', summary='x', pages=[FileSource('long', summary='x', path=page)])

  head = source.read('long')
  assert 'line 0' in head
  assert f'line {PAGE_LIMIT}\n' not in head
  marker = re.search(r'read on with offset=(\d+)', head)
  assert marker is not None
  offset = int(marker.group(1))

  tail = source.read('long', offset=offset)
  # the two reads cover the page exactly — nothing dropped, nothing repeated
  assert head[:offset] + tail == body


def test_offset_past_the_page_raises(source):
  with pytest.raises(ValueError, match='outside the first page'):
    source.read('first', offset=10_000)


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


@pytest.mark.asyncio
async def test_read_tool_rejects_a_non_integer_offset(source):
  tool = (await source.as_mcp_server().list_tools())[0]
  with pytest.raises(ValueError, match='offset'):
    await tool.call({'topic': 'first', 'offset': '2'})


def test_manual_collapses_repeats_in_first_declared_order(tmp_path):
  first = FileSource('first', summary='x', path=tmp_path / 'first.md')
  second = FileSource('second', summary='x', path=tmp_path / 'second.md')
  folded = manual([ManPage(first), ManPage(second), ManPage(first)])
  assert [page.name for page in folded.pages] == ['first', 'second']


def test_man_declares_a_page_of_the_repo_roster():
  entry = references.man('dive-in')
  assert entry.page.name == 'dive-in'
  assert len(entry.page.read()) > 0


def test_man_names_the_topics_on_an_unknown_one():
  with pytest.raises(LookupError, match='dive-in'):
    references.man('no-such-topic')
