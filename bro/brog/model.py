from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

# the brog task lifecycle: workable / succeeded / won't-happen.
Status = Literal['open', 'done', 'dropped']


@dataclass
class Project:
  """a task's project ref; name and summary are None when the backend can no
  longer resolve the project (e.g. it completed and left the active listing)"""

  id: str
  name: Optional[str]
  summary: Optional[str]


@dataclass
class Task:
  """task metadata; ids are opaque strings in the backend's native canonical form

  blocked_by lists ids of *open* blocking tasks and is read-only; an empty list
  means the task is workable. project is None on backends without projects.
  """

  id: str
  name: str
  status: Status
  url: str
  tags: list[str]
  project: Optional[Project]
  blocked_by: list[str]


@dataclass
class Comment:
  """one comment-stream entry: a timestamped development event

  All metadata is the backend's own record, never caller-supplied: topic and author
  are None when the backend recorded none (a comment written outside brog, a write
  with no persona). timestamp is UTC.
  """

  topic: Optional[str]
  author: Optional[str]
  timestamp: datetime
  body: str
