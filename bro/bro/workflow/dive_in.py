#!/usr/bin/env python
"""start a cw session focused on the currently focused task."""

import re
import subprocess
import sys
from pathlib import Path

from base import log
from base.args import Parser
from flow.focus.client.client import default_client


PROMPT = """\
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


def dive_in(dry_run: bool = False, command: str | None = None) -> int:
  client = default_client()
  state = client.get_focus()
  if state is None:
    log.error('no task is currently focused')
    return 1
  name = _slugify(state.task.name)
  if len(name) == 0:
    name = 'dive-in'
  log.info('focused: %s', state.task.name)

  prompt = PROMPT if command is None else f'{PROMPT}\n\nOnce you understand the task, {command}'

  proj = _project_root()
  cw_bin = proj / '.venv' / 'bin' / 'cw'
  cmd = [str(cw_bin), 'ss', '-c', '--mcp', name, prompt]
  if dry_run:
    print(' '.join(_shell_quote(c) for c in cmd))
    return 0
  return subprocess.run(cmd).returncode


def main(argv=None):
  parser = Parser(description='start a cw session focused on the currently focused task')
  parser.add_argument(
    '-n', '--dry-run', action='store_true', help='print the command without running it'
  )
  parser.add_argument(
    'command', nargs='?', default=None, help='initial command for the session (appended to prompt)'
  )
  args = parser.parse(argv)
  return dive_in(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
