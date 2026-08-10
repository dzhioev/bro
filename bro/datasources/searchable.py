import json
from abc import abstractmethod
from dataclasses import asdict, dataclass
from typing import Optional

from pydantic import BaseModel

from bro.base import credentials, template
from bro.datasources.base import DataSource
from bro.llm.mcp import InProcessMCPServer, MCPServer, Tool

# every searchable source summarises a fetched record against the caller's query
# through the one `mu` path, which reads the LLM key. so the query-focused branch
# of `fetch` uniformly depends on this secret — declared best-effort (the raw
# record is still returned when no query is passed), not a hard requirement.
SUMMARY_SECRET = 'openai'


class _Summary(BaseModel):
  summary: str


@dataclass
class Hit:
  id: str
  title: str
  snippet: Optional[str] = None


class SearchableDataSource(DataSource):
  optional_secrets = (SUMMARY_SECRET,)
  # `summary` — the query-focused fetch mode, live iff the LLM key resolves
  feature_names = ('summary',)

  def has_feature(self, name: str) -> bool:
    if name == 'summary':
      return credentials.available(SUMMARY_SECRET)
    raise NotImplementedError(f'{type(self).__name__} declares no feature {name!r}')

  @abstractmethod
  async def search(self, query: str, limit: int = 5) -> list[Hit]: ...

  @abstractmethod
  async def _fetch_content(self, id: str) -> str:
    """return the raw record text for `id` — no summarisation. subclasses
    implement this; the base `fetch` layers the query-focused summary on top."""
    ...

  async def fetch(self, id: str, query: Optional[str] = None) -> str:
    # omit `query` for the raw record; pass it to focus the record on what the
    # caller is investigating. summarisation reads the LLM key, so when it is
    # absent a non-null query errors rather than silently returning raw text —
    # the agent loop turns the raise into a tool result it can retry without the
    # query.
    content = await self._fetch_content(id)
    if query is None or len(query) == 0:
      return content
    if not credentials.available(SUMMARY_SECRET):
      raise ValueError(
        f'the `query` parameter requires the `{SUMMARY_SECRET}` secret, not available '
        'this session; retry with `query` omitted for the raw record'
      )
    # lazy: keep the openai SDK (via mu) out of every datasource import — only the
    # query path needs it.
    from bro.llm.mu import Text, mu
    from bro.prompts import get_prompt

    prompt = get_prompt(
      'source_summary.prompt.template',
      source=self.name,
      id=id,
      query=query,
      content=content,
    )
    summary = await mu.aio(prompt, _Summary, Text(content), reasoning_effort='low')
    return summary.summary

  def as_mcp_server(self) -> MCPServer:
    server = InProcessMCPServer(self.namespace, [_SearchTool(self), _FetchTool(self)])
    # stamp the source's secrets onto the vanilla server (writable class-attr
    # defaults, no property clash) so the live server stays self-describing.
    server.needed_secrets = self.needed_secrets
    server.optional_secrets = self.optional_secrets
    return server


class _SearchTool(Tool):
  def __init__(self, source: SearchableDataSource):
    self._source = source

  @property
  def name(self) -> str:
    return 'search'

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
  # rendered below against the source's own vocabulary, so the text leaves the
  # server fully resolved
  _DESCRIPTION = (
    'fetch a record from the {{insert #source}} data source by id. The optional `query` is '
    'the question you are investigating; given one, the record is summarised to '
    'focus on it'
    '{{iff #features contains summary}} (omit it for the raw record).{{else}} — but '
    'summarisation is unavailable this session, so you should omit `query`; '
    'fetch returns the raw record.{{end}}'
  )

  _QUERY_DESCRIPTION = (
    '{{iff #features contains summary}}original query the caller is investigating; '
    'lets the source focus the result{{else}}unavailable this session '
    '(summarisation is off) — omit and use the raw record{{end}}'
  )

  def __init__(self, source: SearchableDataSource):
    self._source = source

  @property
  def name(self) -> str:
    return 'fetch'

  @property
  def description(self) -> str:
    return template.render(self._DESCRIPTION, self._source.text_variables())

  @property
  def parameters(self) -> dict:
    return {
      'type': 'object',
      'properties': {
        'id': {'type': 'string', 'description': 'record id (e.g. from a prior search hit)'},
        'query': {
          'type': 'string',
          'description': template.render(self._QUERY_DESCRIPTION, self._source.text_variables()),
        },
      },
      'required': ['id'],
    }

  async def call(self, arguments: dict) -> str:
    return await self._source.fetch(arguments['id'], arguments.get('query'))
