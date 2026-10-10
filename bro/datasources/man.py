from collections.abc import Sequence
from typing import Any

from bro.base.name_map import NameMap
from bro.datasources.base import DataSource
from bro.datasources.file import FileSource
from bro.llm.mcp import InProcessMCPServer, MCPServer, Tool

MANUAL_NAME = 'man'
MANUAL_SUMMARY = 'the reference pages this session carries, read on demand by topic'


class ManSource(DataSource):
  """serve a roster of static reference pages as one `read(topic)` tool.

  Each page is a `FileSource` — its `name` is the topic, its `summary` the line
  the roster shows, its body what `read` returns — so one declaration serves a
  doc either as its own dedicated `read` tool or as a topic here. The roster
  and its summaries ride the tool description, so a surface that sees only the
  tool listing still knows what can be read; a topic that matches nothing
  raises with the roster listed.
  """

  def __init__(self, name: str, summary: str, pages: Sequence[FileSource]):
    if len(pages) == 0:
      raise ValueError(f'man source {name!r} declares no pages')
    self.name = name
    self.summary = summary
    self.pages = list(pages)
    self._by_topic = NameMap({page.name: page for page in pages})

  def read(self, topic: str) -> str:
    return self._by_topic.resolve(topic).read()

  def as_mcp_server(self) -> MCPServer:
    return InProcessMCPServer(self.namespace, [_ReadTool(self)])


def manual(pages: Sequence[FileSource]) -> ManSource:
  """the manual a bro's declared pages amount to."""
  return ManSource(MANUAL_NAME, MANUAL_SUMMARY, pages)


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
      },
      'required': ['topic'],
    }

  async def call(self, arguments: dict[str, Any]) -> str:
    return self._source.read(arguments['topic'])
