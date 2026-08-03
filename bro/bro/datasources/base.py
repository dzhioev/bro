from abc import ABC, abstractmethod

from bro.base import template
from bro.base.condition import SetVariable, StringVariable, Variables
from bro.llm.mcp import MCPServer


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
  # the source's own rendering vocabulary: feature names its static text (the
  # summary, tool descriptions and parameter annotations) may test with a
  # `#features contains <name>` directive — capabilities, not the credentials
  # or harness facts behind them, so the text reads the same served standalone.
  # `has_feature` reports which currently hold; it is probed lazily at render
  # time, so declaring a source stays an import-time constant.
  feature_names: tuple[str, ...] = ()

  def has_feature(self, name: str) -> bool:
    """whether the named feature currently holds; probed only for names in
    `feature_names` (the closed universe), so a source that declares features
    must override this."""
    raise NotImplementedError(f'{type(self).__name__} declares no feature {name!r}')

  def text_variables(self) -> Variables:
    """the variables this source's static text renders against: `#features`
    plus `#source` — the source's own name, for `{{insert #source}}`."""
    return {
      'features': SetVariable(self.has_feature, universe=frozenset(self.feature_names)),
      'source': StringVariable(self.name),
    }

  def rendered_summary(self) -> str:
    """`summary` with its directives rendered against the live vocabulary."""
    if '{{' not in self.summary:
      return self.summary
    return template.render(self.summary, self.text_variables())

  @property
  def namespace(self) -> str:
    # the `-source` suffix keeps generic, collision-prone tool names (`search`,
    # `fetch`, `read`) distinct per source without nested namespaces: the
    # category *is* the source. stamped onto whatever `as_mcp_server()` returns.
    return f'{self.name}-source'

  @abstractmethod
  def as_mcp_server(self) -> MCPServer: ...
