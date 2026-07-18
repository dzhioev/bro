import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Optional

from base import credentials
from brog.model import Comment, Status, Task


class System(ABC):
  """backend surface of the brog task tracker

  Deliberately narrower than a full tracker: exactly what the dev workflow needs —
  create, read, close, comment. Ids are opaque strings in the backend's native
  canonical form; each op accepts the backend's natural refs (UUID, URL, issue
  number, ...) and returns the canonical form.
  """

  @abstractmethod
  def create_task(
    self, *, name: str, body: Optional[str] = None, tags: Optional[list[str]] = None
  ) -> Task:
    """create a task and return it; tasks are born open (workable)"""
    ...

  @abstractmethod
  def get_task(self, task_id: str) -> Task:
    """return task metadata (no document body)"""
    ...

  @abstractmethod
  def get_task_description(self, task_id: str) -> str:
    """return the task description as markdown — the comment stream is separate
    (get_task_comments)"""
    ...

  @abstractmethod
  def get_task_comments(self, task_id: str) -> list[Comment]:
    """return the task's comment stream, oldest first; empty when the task has no
    comments"""
    ...

  @abstractmethod
  def update_task(
    self,
    task_id: str,
    *,
    name: Optional[str] = None,
    status: Optional[Status] = None,
    tags: Optional[list[str]] = None,
  ) -> None:
    """update task properties; None means don't touch ([] clears tags)"""
    ...

  @abstractmethod
  def add_comment(self, task_id: str, topic: str, body: str) -> None:
    """append a comment entry to the task's comment stream

    The entry's author and timestamp are the backend's own record (the hosting persona
    or acting account, at the moment of writing) — never parameters.
    """
    ...

  @abstractmethod
  def append_description(self, task_id: str, markdown: str) -> None:
    """append markdown to the task description — before the comment stream, never
    into it"""
    ...

  @abstractmethod
  def edit_description(
    self, task_id: str, old_string: str, new_string: str, replace_all: bool = False
  ) -> int:
    """replace old_string with new_string in the task description; returns the number
    of occurrences replaced

    The match scope is the description only, never the comment stream — comments are
    append-only history; a match reaching into them is an error. old_string must occur
    exactly once unless replace_all is true.
    """
    ...

  @abstractmethod
  def list_tasks(
    self,
    *,
    status: Optional[Status] = None,
    project: Optional[str] = None,
    limit: int = 20,
  ) -> list[Task]:
    """query tasks; None filters match any. No pagination — raise the limit instead"""
    ...


def build_system(
  config_provider: Callable[[], dict[str, Any]], *, author: Optional[str] = None
) -> System:
  """construct the backend the provided config selects; author is the persona
  stamped on comments (ignored by backends that record authorship natively)

  Backend selection reads the provider once; a backend whose credential may be
  minted short-lived (the github token) keeps the provider and re-reads it per
  operation, observing a fresh expansion instead of the one baked at build.
  """
  config = config_provider()
  backend = _required(config, 'backend')
  if backend == 'flow':
    return _flow_system(config, author)
  if backend == 'github':
    return _github_system(config, config_provider)
  raise ValueError(f'unknown brog backend {backend!r}; known: flow, github')


def _flow_system(config: dict[str, Any], author: Optional[str]) -> System:
  import brog.flow_proxy

  transport = _required(config, 'transport')
  if transport == 'http':
    url = _required(config, 'url')
    token = _required(config, 'token')
    return brog.flow_proxy.System(brog.flow_proxy.HTTPTransport(url, token), author=author)
  if transport == 'local':
    notion = _required(config, 'notion')
    if not isinstance(notion, dict):
      raise ValueError('brog config: "notion" must be an object (the notion.json shape)')
    from flow.notion.system import System as NotionSystem
    from notion.notion import NotionAPI

    return brog.flow_proxy.System(
      brog.flow_proxy.LocalTransport(NotionSystem(api=NotionAPI(notion))), author=author
    )
  raise ValueError(f'unknown brog flow transport {transport!r}; known: http, local')


def _github_system(config: dict[str, Any], config_provider: Callable[[], dict[str, Any]]) -> System:
  import brog.github

  _required(config, 'token')  # a config without a token fails at build, not at first use
  repo = config.get('repo')
  if repo is None:
    repo = brog.github.origin_repo()

  def token() -> str:
    return _required(config_provider(), 'token')

  return brog.github.System(token=token, repo=repo)


def _required(config: dict[str, Any], key: str) -> Any:
  value = config.get(key)
  if value is None:
    raise ValueError(f'brog config is missing {key!r}')
  return value


def default_system() -> System:
  """the backend selected by the `brog` config

  The config is self-contained — every credential the active backend needs is embedded,
  literally or via `$cred` references the resolver expands — so brog needs no secret
  granted beyond `brog`. The comment author is the session persona (`CW_BRO`); with no
  persona, comments carry no author segment.
  """
  author = os.environ.get('CW_BRO')
  if author == '':
    author = None
  return build_system(lambda: credentials.get_json('brog'), author=author)
