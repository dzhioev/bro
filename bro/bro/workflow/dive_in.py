#!/usr/bin/env python
"""start a cw session focused on a task."""

import re
import subprocess
import sys
from pathlib import Path

from base import log
from base.args import Parser
from flow.focus.client.client import default_client


FOCUSED_PROMPT = """\
Dive into the currently focused task and figure out how to accomplish it.

Step 1 — understand the task:
- get_focused_task to find what is currently focused (if nothing is focused, say so and stop)
- get_task_info for full metadata
- get_content for the task body

Step 2 — gather context:
- If the task has a project: look it up in get_projects for its summary, then list_tasks \
filtered to that project (active statuses: Live, Waiting, Repeated) to see sibling tasks
- Note any tags — they classify the task domain

Step 3 — plan:
Synthesize what you learned. What is this task about, what is the goal, what is the project \
context. Figure out how to achieve it — for coding tasks, explore the codebase; for tasks that \
need external information, say what you need. Present your understanding and proposed approach, \
then start working."""


TASK_PROMPT = """\
Dive into task {task_id} and figure out how to accomplish it.

Step 1 — understand the task:
- get_task_info("{task_id}") for full metadata
- get_content("{task_id}") for the task body

Step 2 — gather context:
- If the task has a project: look it up in get_projects for its summary, then list_tasks \
filtered to that project (active statuses: Live, Waiting, Repeated) to see sibling tasks
- Note any tags — they classify the task domain

Step 3 — plan:
Synthesize what you learned. What is this task about, what is the goal, what is the project \
context. Figure out how to achieve it — for coding tasks, explore the codebase; for tasks that \
need external information, say what you need. Present your understanding and proposed approach, \
then start working."""


def _slugify(name: str) -> str:
  s = name.lower().strip()
  s = re.sub(r'[^a-z0-9]+', '-', s)
  s = s.strip('-')
  return s[:40].rstrip('-') if len(s) > 40 else s


def _project_root() -> Path:
  return (
    Path(subprocess.check_output(['git', 'rev-parse', '--git-common-dir'], text=True).strip())
    .resolve()
    .parent
  )


def _shell_quote(s: str) -> str:
  if re.fullmatch(r'[A-Za-z0-9_./:@=-]+', s):
    return s
  return "'" + s.replace("'", "'\\''") + "'"


_NOTION_URL_RE = re.compile(r'https?://(?:www\.)?notion\.(?:so|site)/.+-([0-9a-f]{32})(?:\?.*)?$')
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _resolve_task_id(task_ref: str) -> str:
  m = _NOTION_URL_RE.match(task_ref)
  if m is not None:
    raw = m.group(1)
    return f'{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}'
  if _UUID_RE.match(task_ref) is not None:
    return task_ref
  raise ValueError(f'--task must be a Notion URL or a UUID task ID: {task_ref}')


def _resolve_task_name(task_id: str) -> str:
  from flow.system import default_system

  system = default_system()
  task = system.get_task_info(task_id)
  return task.name


def dive_in(dry_run: bool = False, command: str | None = None, task: str | None = None) -> int:
  if task is not None:
    task_id = _resolve_task_id(task)
    task_name = _resolve_task_name(task_id)
    log.info('task: %s', task_name)
    prompt = TASK_PROMPT.format(task_id=task_id)
  else:
    client = default_client()
    state = client.get_focus()
    if state is None:
      log.error('no task is currently focused')
      return 1
    task_name = state.task.name
    log.info('focused: %s', task_name)
    prompt = FOCUSED_PROMPT

  name = _slugify(task_name)
  if len(name) == 0:
    name = 'dive-in'

  if command is not None:
    prompt = f'{prompt}\n\nOnce you understand the task, {command}'

  proj = _project_root()
  cw_bin = proj / '.venv' / 'bin' / 'cw'
  cmd = [str(cw_bin), 'ss', '-c', '--mcp', name, prompt]
  if dry_run:
    print(' '.join(_shell_quote(c) for c in cmd))
    return 0
  return subprocess.run(cmd).returncode


def main(argv=None):
  parser = Parser(description='start a cw session focused on a task')
  parser.add_argument(
    '-n', '--dry-run', action='store_true', help='print the command without running it'
  )
  parser.add_argument(
    '-t', '--task', default=None, help='task ID or Notion URL to dive into (default: focused task)'
  )
  parser.add_argument(
    'command', nargs='?', default=None, help='initial command for the session (appended to prompt)'
  )
  args = parser.parse(argv)
  return dive_in(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
