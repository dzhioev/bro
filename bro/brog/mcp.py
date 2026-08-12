from dataclasses import dataclass
from typing import Annotated, Optional

from pydantic import Field

from bro.base.text_window import DEFAULT_LIMIT, MAX_LIMIT, numbered_window
from bro.brog.model import Comment, Status, Task
from bro.brog.system import System, default_system
from bro.llm.mcp import Context, Toolset


class _Toolset(Toolset[System]):
  # the brog config is self-contained (the active backend's credentials are embedded),
  # so the manifest is static — no tool-subset derivation
  secrets = ('brog',)


toolset = _Toolset('brog', state=default_system)

_TASK_ID_FIELD = Field(
  description=(
    "task id — opaque; any of the backend's natural refs is accepted "
    '(canonical id, URL), the canonical form is returned'
  ),
)


@dataclass
class CreatedTask:
  id: str
  url: str


@toolset.tool(
  "create a task; tasks are born open (workable). Returns the created task's canonical id and url"
)
def create_task(
  context: Context[System],
  name: Annotated[
    str,
    Field(
      description=('task name. Names should start with a lowercase letter (except proper nouns).'),
    ),
  ],
  body: Annotated[
    Optional[str],
    Field(description='markdown body for the task description; omit for an empty one'),
  ] = None,
  tags: Annotated[
    Optional[list[str]],
    Field(description='tags classifying the task'),
  ] = None,
) -> CreatedTask:
  task = context.state.create_task(name=name, body=body, tags=tags)
  return CreatedTask(id=task.id, url=task.url)


@toolset.tool(
  'return task metadata: status (open = workable, done, dropped), url, tags, the '
  'owning project (with its summary) when there is one, and blocked_by — ids of '
  'still-open tasks blocking this one (empty = workable). No document content; use '
  'read_task / read_comments for that.'
)
def get_task(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
) -> Task:
  return context.state.get_task(task_id)


@toolset.tool(
  'read a window of the task description rendered as markdown, each line prefixed '
  'with its 1-based line number (cat -n style). Content outside the window is '
  'announced with [...skipped before/after: N lines...] markers. Line numbers are '
  'orientation only, not part of the description — strip the "N<tab>" prefix before '
  'reusing text. The comment stream is separate; use read_comments for it.'
)
def read_task(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
  offset: Annotated[
    int,
    Field(description='0-based line index to start reading from', ge=0),
  ] = 0,
  limit: Annotated[
    int,
    Field(
      description=(
        f'max lines to return; values above {MAX_LIMIT:,} are clamped, with the clamp '
        'announced inline'
      ),
    ),
  ] = DEFAULT_LIMIT,
) -> str:
  return numbered_window(context.state.get_task_description(task_id), offset, limit)


@toolset.tool(
  "return the task's comment stream, oldest first: entries with topic, author, UTC "
  'timestamp, and markdown body — the durable record of development events. topic '
  'and author are null when the backend recorded none (e.g. a comment written '
  'outside brog). Empty when the task has no comments.'
)
def read_comments(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
) -> list[Comment]:
  return context.state.get_task_comments(task_id)


@toolset.tool(
  'update task name, status, and/or tags; an omitted (or null) property is left '
  'untouched ([] clears tags). Returns "ok".'
)
def update_task(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
  name: Annotated[
    Optional[str],
    Field(description='new task name'),
  ] = None,
  status: Annotated[
    Optional[Status],
    Field(
      description=(
        "new status: done = completed successfully, dropped = won't happen (no longer "
        'relevant), open reopens a closed task'
      ),
    ),
  ] = None,
  tags: Annotated[
    Optional[list[str]],
    Field(description='replacement tags; [] clears'),
  ] = None,
) -> str:
  context.state.update_task(task_id, name=name, status=status, tags=tags)
  return 'ok'


@toolset.tool(
  'append a comment entry to the task — the durable record of a development event '
  '(a plan, a design change, a blocker, a merge). The author and timestamp are the '
  'backend\'s own record of the write, never parameters. Returns "ok".'
)
def add_comment(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
  topic: Annotated[
    str,
    Field(description='short lowercase topic for the entry heading (e.g. "plan", "merged")'),
  ],
  body: Annotated[
    str,
    Field(description='markdown body of the entry; keep it to the why, 2-5 lines'),
  ],
) -> str:
  context.state.add_comment(task_id, topic, body)
  return 'ok'


@toolset.tool(
  'append markdown to the task description — document sections like `## Design` or '
  '`## Verification` land here. The comment stream stays below it; use add_comment '
  'for events. Returns "ok".'
)
def append_description(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
  markdown: Annotated[str, Field(description='markdown to append to the description')],
) -> str:
  context.state.append_description(task_id, markdown)
  return 'ok'


@toolset.tool(
  'replace old_string with new_string in the task description — for in-place section '
  'updates. old_string is matched against the description markdown read_task returns '
  '(minus the line-number prefixes) and must occur exactly once — errors when absent, '
  'and when ambiguous unless replace_all=true (which replaces every occurrence). The '
  'match scope is the description only: comments are append-only history and are '
  'never touched. Returns "replaced N occurrence(s)".'
)
def edit_description(
  context: Context[System],
  task_id: Annotated[str, _TASK_ID_FIELD],
  old_string: Annotated[
    str,
    Field(
      description=(
        'exact text to replace, copied from the description markdown read_task returns '
        '(strip the "N<tab>" line-number prefixes first). Must occur exactly once '
        'unless replace_all is true.'
      ),
    ),
  ],
  new_string: Annotated[
    str,
    Field(description='replacement text; empty string deletes the matched text'),
  ],
  replace_all: Annotated[
    bool,
    Field(description='replace every occurrence instead of requiring a unique match'),
  ] = False,
) -> str:
  count = context.state.edit_description(task_id, old_string, new_string, replace_all=replace_all)
  return f'replaced {count} occurrence(s)'


@toolset.tool('query tasks — sibling context for the one being worked; omitted filters match any')
def list_tasks(
  context: Context[System],
  status: Annotated[
    Optional[Status],
    Field(description='status to match'),
  ] = None,
  project: Annotated[
    Optional[str],
    Field(description='project id to match'),
  ] = None,
  limit: Annotated[
    int,
    Field(description='max results (1-100)', ge=1, le=100),
  ] = 20,
) -> list[Task]:
  return context.state.list_tasks(status=status, project=project, limit=limit)
