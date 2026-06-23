from abc import ABC, abstractmethod

from llm.mcp import MCPServer


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


class DataSource(ABC):
  name: str
  summary: str
  # credentials this source resolves through the store. unioned (along the MRO)
  # into `bro.needed_secrets()` so the host can hydrate a scoped credential set
  # per bro. override with the API key a subclass reads (e.g. TMDb → `tmdb`); the
  # empty default means "no credentials" (e.g. Wikipedia, OpenLibrary).
  needed_secrets: tuple[str, ...] = ()
  # credentials this source uses *if present* but degrades without (e.g. the LLM
  # key behind a query-focused fetch summary). unioned into
  # `bro.optional_secrets()`, hydrated best-effort by the host. mirrors
  # `needed_secrets`.
  optional_secrets: tuple[str, ...] = ()

  @property
  def namespace(self) -> str:
    # the `-source` suffix keeps generic, collision-prone tool names (`search`,
    # `fetch`, `read`) distinct per source without nested namespaces: the
    # category *is* the source. stamped onto whatever `as_mcp_server()` returns.
    return f'{self.name}-source'

  @abstractmethod
  def as_mcp_server(self) -> MCPServer: ...
