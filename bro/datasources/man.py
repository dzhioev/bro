from collections.abc import Sequence
from typing import Any

from bro.base.name_map import NameMap
from bro.base.text_window import format_size, take_head
from bro.datasources.base import DataSource
from bro.datasources.file import FileSource
from bro.llm.mcp import InProcessMCPServer, MCPServer, Tool

# sized so an ordinary page arrives whole and only a long reference is walked
# across calls
PAGE_LIMIT = 200


class ManSource(DataSource):
  """serve a roster of static reference pages as one `read(topic)` tool.

  Each page is a `FileSource` — its `name` is the topic, its `summary` the line
  the roster shows, its body what `read` returns — so one declaration serves a
  doc either as its own dedicated `read` tool or as a topic here. The roster
  and its summaries ride the tool description, so a surface that sees only the
  tool listing still knows what can be read; a topic that matches nothing
  raises with the roster listed.

  Output is capped at `PAGE_LIMIT` lines, and a page longer than that closes on
  the `offset` to resume at, so the rest is read across successive calls with
  nothing dropped and no counting to do.
  """

  def __init__(self, name: str, summary: str, pages: Sequence[FileSource]):
    if len(pages) == 0:
      raise ValueError(f'man source {name!r} declares no pages')
    self.name = name
    self.summary = summary
    self.pages = list(pages)
    self._by_topic = NameMap({page.name: page for page in pages})

  def read(self, topic: str, offset: int = 0) -> str:
    page = self._by_topic.resolve(topic)
    body = page.read()
    if not 0 <= offset <= len(body):
      raise ValueError(f'offset {offset} is outside the {page.name} page (0..{len(body)})')
    kept, _ = take_head(body[offset:], PAGE_LIMIT)
    remaining = len(body) - offset - len(kept)
    if remaining == 0:
      return kept
    return (
      f'{kept}\n\n[...{format_size(remaining)} left — read on with offset={offset + len(kept)}...]'
    )

  def as_mcp_server(self) -> MCPServer:
    return InProcessMCPServer(self.namespace, [_ReadTool(self)])


class _ReadTool(Tool):
  def __init__(self, source: ManSource):
    self._source = source

  @property
  def name(self) -> str:
    return 'read'

  @property
  def description(self) -> str:
    lines = [
      f'return one of the {self._source.name} pages — {self._source.rendered_summary()}',
      '',
      'Topics:',
    ]
    lines.extend(f'- `{page.name}` — {page.rendered_summary()}' for page in self._source.pages)
    lines.append('')
    lines.append(
      'A page too long to return at once closes on a marker naming the `offset` to resume '
      'at; call again with it for the rest.'
    )
    return '\n'.join(lines)

  @property
  def parameters(self) -> dict[str, Any]:
    return {
      'type': 'object',
      'properties': {
        'topic': {
          'type': 'string',
          'enum': [page.name for page in self._source.pages],
          'description': 'the page to return',
        },
        'offset': {
          'type': 'integer',
          'default': 0,
          'description': 'where to resume a page a previous call could not return whole — '
          "the value that call's closing marker named",
        },
      },
      'required': ['topic'],
    }

  async def call(self, arguments: dict[str, Any]) -> str:
    offset = arguments.get('offset', 0)
    if isinstance(offset, bool) or not isinstance(offset, int):
      raise ValueError('"offset" must be an integer')
    return self._source.read(arguments['topic'], offset=offset)
