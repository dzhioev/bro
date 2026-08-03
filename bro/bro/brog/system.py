import importlib.metadata
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Optional, cast

from bro.base import credentials
from bro.brog.model import Comment, Status, Task


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


_BACKEND_ENTRY_POINT_GROUP = 'bro.brog.backends'
BackendFactory = Callable[
  [Callable[[], dict[str, Any]], dict[str, Any], Optional[str]],
  System,
]


def _backend_entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=_BACKEND_ENTRY_POINT_GROUP))


def _backend_factory(name: str) -> BackendFactory:
  if name == 'github':
    return _github_system
  matches = [entry_point for entry_point in _backend_entry_points() if entry_point.name == name]
  if len(matches) > 1:
    values = ', '.join(entry_point.value for entry_point in matches)
    raise ValueError(f'duplicate brog backend {name!r}: {values}')
  if len(matches) == 0:
    known = sorted({'github'} | {entry_point.name for entry_point in _backend_entry_points()})
    raise ValueError(f'unknown brog backend {name!r}; known: {", ".join(known)}')
  factory = matches[0].load()
  if not callable(factory):
    raise TypeError(f'brog backend entry point {name!r} must load a callable')
  return cast(BackendFactory, factory)


def build_system(
  config_provider: Callable[[], dict[str, Any]], *, author: Optional[str] = None
) -> System:
  """construct the backend the provided config selects.

  Backend selection reads the provider once. A backend whose credential may be
  minted short-lived can retain the provider and re-read it per operation.
  """
  config = config_provider()
  backend = _required(config, 'backend')
  return _backend_factory(backend)(config_provider, config, author)


def _github_system(
  config_provider: Callable[[], dict[str, Any]],
  config: dict[str, Any],
  author: Optional[str],
) -> System:

  import bro.brog.github as brog_github

  _required(config, 'token')
  repo = config.get('repo')
  if repo is None:
    repo = brog_github.origin_repo()

  def token() -> str:
    return _required(config_provider(), 'token')

  return brog_github.System(token=token, repo=repo)


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
