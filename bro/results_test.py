import re
from pathlib import Path

import pytest

from bro import results
from bro.base.text_window import BYTE_LIMIT
from bro.llm.mcp import FunctionTool, InProcessMCPServer, ToolRegistry
from bro.monitor import SESSION_DIR_ENV

_MARKER = re.compile(
  r'\n\[\.\.\.([\d,]+) more characters — bro::page\(result="([^"]+)", char_offset=(\d+)\)\.\.\.\]$'
)


def _pages(store: results.Store, result_id: str, first: str) -> list[str]:
  # follow every marker from `first` to the end, as a model reading on would
  pages = []
  reply = first
  while (match := _MARKER.search(reply)) is not None:
    assert match.group(2) == result_id
    pages.append(reply[: match.start()])
    _, text = store.read(result_id)
    reply = results.window(result_id, text, int(match.group(3)), BYTE_LIMIT)
  pages.append(reply)
  return pages


class TestStore:
  def test_keeps_whole_results_under_sequential_ids(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    first = store.keep('cli::rewind_show', {'trail_id': 't'}, 'one')
    second = store.keep('spell::fix', {}, 'two')
    tag = store.tag()
    assert (first, second) == (f'{tag}-1', f'{tag}-2')
    entry, text = store.read(first)
    assert text == 'one'
    assert entry == results.Entry(first, 'cli::rewind_show', {'trail_id': 't'}, 3)

  def test_keeps_text_whose_first_line_looks_like_a_header(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    text = '{"tool": "x"}\nsecond line\n'
    assert store.read(store.keep('a::b', {}, text))[1] == text

  def test_a_second_store_over_the_directory_continues_without_overwriting(self, tmp_path):
    directory = tmp_path / 'results'
    earlier = results.Store(directory)
    kept = earlier.keep('a::b', {}, 'before the restart')
    later = results.Store(directory)
    assert later.tag() == earlier.tag()
    assert later.keep('a::b', {}, 'after') == f'{later.tag()}-2'
    assert later.read(kept)[1] == 'before the restart'

  def test_stores_sharing_a_directory_never_claim_one_number(self, tmp_path):
    directory = tmp_path / 'results'
    one, other = results.Store(directory), results.Store(directory)
    ids = [one.keep('a::b', {}, 'x'), other.keep('a::b', {}, 'y'), one.keep('a::b', {}, 'z')]
    assert len(set(ids)) == 3
    assert [one.read(result_id)[1] for result_id in ids] == ['x', 'y', 'z']

  def test_refuses_an_id_another_store_minted(self, tmp_path):
    foreign = results.Store(tmp_path / 'other').keep('a::b', {}, 'x')
    store = results.Store(tmp_path / 'results')
    with pytest.raises(ValueError, match='kept by another session'):
      store.read(foreign)

  def test_refuses_an_id_it_never_minted(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    with pytest.raises(ValueError, match='kept no result'):
      store.read(f'{store.tag()}-7')

  def test_refuses_a_malformed_id(self, tmp_path):
    with pytest.raises(ValueError, match='is not a result id'):
      results.Store(tmp_path / 'results').read('r7')

  def test_lists_entries_newest_first(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    assert store.entries() == []
    first = store.keep('a::b', {'n': 1}, 'x')
    second = store.keep('c::d', {'n': 2}, 'yy')
    assert [entry.id for entry in store.entries()] == [second, first]


class TestSessionStore:
  def test_lives_in_the_session_state_dir(self, tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    results.session_store().keep('a::b', {}, 'x')
    assert any((tmp_path / results.DIRNAME).glob('*.result'))

  def test_refuses_outside_a_managed_session(self, monkeypatch):
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)
    with pytest.raises(RuntimeError, match=SESSION_DIR_ENV):
      results.session_store()


class TestWindow:
  def test_returns_a_short_result_whole(self):
    assert results.window('k-1', 'short\n', 0, BYTE_LIMIT) == 'short\n'

  def test_closes_on_the_offset_that_reads_on_after_a_line_of_its_own(self):
    text = 'aaaa\nbbbb\ncccc\n'
    assert results.window('k-1', text, 0, 10) == (
      'aaaa\nbbbb\n\n[...5 more characters — bro::page(result="k-1", char_offset=10)...]'
    )
    assert results.window('k-1', text, 10, 10) == 'cccc\n'

  def test_cuts_a_line_longer_than_the_window_at_the_limit(self):
    reply = results.window('k-1', 'x' * 25, 0, 10)
    assert (
      reply == 'x' * 10 + '\n[...15 more characters — bro::page(result="k-1", char_offset=10)...]'
    )

  def test_pages_reassemble_the_result_unchanged(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    text = 'é' * (BYTE_LIMIT * 2) + '\n' + ''.join(f'line {index}\n' for index in range(5_000))
    result_id = store.keep('a::b', {}, text)
    pages = _pages(store, result_id, results.window(result_id, text, 0, BYTE_LIMIT))
    assert len(pages) > 3
    assert ''.join(pages) == text

  def test_keeps_every_line_ending_as_it_was(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    text = 'crlf\r\ncr\rlf\n'
    assert store.read(store.keep('a::b', {}, text))[1] == text

  def test_pages_reassemble_crlf_text_past_one_window(self, tmp_path):
    store = results.Store(tmp_path / 'results')
    text = 'x\r\n' * 10_001
    result_id = store.keep('a::b', {}, text)
    pages = _pages(store, result_id, results.window(result_id, text, 0, BYTE_LIMIT))
    assert [len(page) for page in pages] == [BYTE_LIMIT, len(text) - BYTE_LIMIT]
    assert ''.join(pages) == text

  def test_refuses_an_offset_outside_the_result(self):
    with pytest.raises(ValueError, match='outside result k-1'):
      results.window('k-1', 'abc', 4, 10)


def short() -> str:
  return 'short'


def lengthy() -> str:
  return ''.join(f'line {index}\n' for index in range(BYTE_LIMIT // 5))


def structured() -> dict[str, str]:
  return {'body': 'x' * BYTE_LIMIT}


short.description = 'short'  # pyright: ignore[reportFunctionMemberAccess]
lengthy.description = 'long'  # pyright: ignore[reportFunctionMemberAccess]
structured.description = 'structured'  # pyright: ignore[reportFunctionMemberAccess]


@pytest.fixture
def kept_registry(tmp_path):
  store = results.Store(tmp_path / 'results')
  server = InProcessMCPServer(
    'test',
    [FunctionTool(short), FunctionTool(lengthy), FunctionTool(structured)],
  )
  service = InProcessMCPServer('bro', results.store_tools(lambda: store))
  return store, ToolRegistry(results.kept([server, service], lambda: store))


class TestKeptTools:
  @pytest.mark.asyncio
  async def test_a_result_that_fits_returns_unchanged_and_is_kept(self, kept_registry):
    store, registry = kept_registry
    assert await registry.call('test__short', {}) == 'short'
    (entry,) = store.entries()
    assert (entry.tool, store.read(entry.id)[1]) == ('test::short', 'short')

  @pytest.mark.asyncio
  async def test_a_long_result_closes_on_the_page_call_that_continues_it(self, kept_registry):
    store, registry = kept_registry
    reply = await registry.call('test__lengthy', {})
    (entry,) = store.entries()
    _, text = store.read(entry.id)
    match = _MARKER.search(reply)
    assert match is not None
    page = await registry.call(
      'bro__page', {'result': entry.id, 'char_offset': int(match.group(3))}
    )
    assert reply[: match.start()] + page == text
    assert len(store.entries()) == 1

  @pytest.mark.asyncio
  async def test_a_long_structured_result_pages_as_json_text(self, kept_registry):
    store, registry = kept_registry
    reply = await registry.call('test__structured', {})
    assert isinstance(reply, str)
    assert reply.startswith('{\n  "body": "xxx')
    assert _MARKER.search(reply) is not None

  @pytest.mark.asyncio
  async def test_kept_tools_declare_no_output_schema(self, kept_registry):
    _, registry = kept_registry
    tools = {tool.name: tool for tool in await registry.resolve()}
    assert FunctionTool(structured).output_schema is not None
    assert tools['test__structured'].output_schema is None

  @pytest.mark.asyncio
  async def test_results_lists_kept_calls_newest_first(self, kept_registry):
    store, registry = kept_registry
    await registry.call('test__short', {})
    await registry.call('test__lengthy', {})
    listing = await registry.call('bro__results', {})
    assert isinstance(listing, str)
    newest, oldest = listing.splitlines()
    tag = store.tag()
    assert newest.startswith(f'{tag}-2  test::lengthy  {{}}  ')
    assert oldest == f'{tag}-1  test::short  {{}}  5 characters'

  @pytest.mark.asyncio
  async def test_results_lists_on_past_one_reply_down_to_the_earliest_call(self, kept_registry):
    store, registry = kept_registry
    earliest = store.keep('test::short', {'query': 'the one to find'}, 'x')
    for index in range(300):
      store.keep('test::short', {'query': f'{index:03} ' + 'q' * 100}, 'x')
    listed: list[str] = []
    listing = await registry.call('bro__results', {})
    assert isinstance(listing, str)
    while (match := re.search(r'bro::results\(before="([^"]+)"\)\.\.\.\]$', listing)) is not None:
      listed.extend(listing.splitlines()[:-1])
      listing = await registry.call('bro__results', {'before': match.group(1)})
      assert isinstance(listing, str)
    listed.extend(listing.splitlines())
    assert [line.split()[0] for line in listed] == [entry.id for entry in store.entries()]
    assert listed[-1].startswith(f'{earliest}  test::short  {{"query": "the one to find"}}')

  @pytest.mark.asyncio
  async def test_page_refuses_a_limit_past_the_window(self, kept_registry):
    store, registry = kept_registry
    result_id = store.keep('a::b', {}, 'x')
    with pytest.raises(Exception, match='less than or equal'):
      await registry.call('bro__page', {'result': result_id, 'char_limit': BYTE_LIMIT + 1})


def test_temporary_store_is_removed_with_its_block():
  with results.temporary_store() as store:
    store.keep('a::b', {}, 'x')
    directory = store.directory
    assert directory.is_dir()
  assert not Path(directory).exists()
