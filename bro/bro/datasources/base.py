import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from llm.mcp import InProcessMCPServer, MCPServer, Tool


class SourceUnavailable(Exception):
  """a data source can't respond (HTTP error, timeout, rate limit, ...).

  the LLM agent loop catches this and feeds the message back to the model as
  the tool result, so the agent can fall back to another source instead of
  crashing the run.
  """

  def __init__(self, source: str, reason: str):
    super().__init__(f'{source} unavailable: {reason}')
    self.source = source
    self.reason = reason


@dataclass
class Hit:
  id: str
  title: str
  snippet: str | None = None


class DataSource(ABC):
  name: str
  summary: str
  # credentials this source resolves through the store. unioned (along the MRO)
  # into `bro.needed_secrets()` so the host can hydrate a scoped credential set
  # per bro. override with the API key a subclass reads (e.g. TMDb → `tmdb`); the
  # empty default means "no credentials" (e.g. Wikipedia, OpenLibrary).
  needed_secrets: tuple[str, ...] = ()

  @abstractmethod
  def as_mcp_server(self) -> MCPServer: ...


class SearchableDataSource(DataSource):
  @abstractmethod
  async def search(self, query: str, limit: int = 5) -> list[Hit]: ...

  @abstractmethod
  async def fetch(self, id: str, query: str | None = None) -> str: ...

  def as_mcp_server(self) -> MCPServer:
    return InProcessMCPServer([_SearchTool(self), _FetchTool(self)])


class _SearchTool(Tool):
  def __init__(self, source: SearchableDataSource):
    self._source = source

  @property
  def name(self) -> str:
    return f'{self._source.name}-search'

  @property
  def description(self) -> str:
    return (
      f'search the {self._source.name} data source; returns a list of hits with id, title, snippet'
    )

  @property
  def parameters(self) -> dict:
    return {
      'type': 'object',
      'properties': {
        'query': {'type': 'string', 'description': 'search query'},
        'limit': {
          'type': 'integer',
          'description': 'maximum number of hits to return',
          'default': 5,
        },
      },
      'required': ['query'],
    }

  async def call(self, arguments: dict) -> str:
    limit = arguments.get('limit', 5)
    hits = await self._source.search(arguments['query'], limit)
    return json.dumps([asdict(h) for h in hits])


class _FetchTool(Tool):
  def __init__(self, source: SearchableDataSource):
    self._source = source

  @property
  def name(self) -> str:
    return f'{self._source.name}-fetch'

  @property
  def description(self) -> str:
    return (
      f'fetch a record from the {self._source.name} data source by id. '
      'Pass the original query so the source can return a focused summary'
    )

  @property
  def parameters(self) -> dict:
    return {
      'type': 'object',
      'properties': {
        'id': {'type': 'string', 'description': 'record id (e.g. from a prior search hit)'},
        'query': {
          'type': 'string',
          'description': 'original query the caller is investigating; lets the source focus the result',
        },
      },
      'required': ['id'],
    }

  async def call(self, arguments: dict) -> str:
    return await self._source.fetch(arguments['id'], arguments.get('query'))
