#!/usr/bin/env python
"""start a cw session focused on a task."""

import os
import re
import secrets
import subprocess
from typing import Optional

import cw
from base import log
from base.args import Parser
from flow.focus.client.client import default_client
from notion import parse_page_ref


def _slugify(name: str) -> str:
  s = name.lower().strip()
  s = re.sub(r'[^a-z0-9]+', '-', s)
  s = s.strip('-')
  return s[:40].rstrip('-') if len(s) > 40 else s


def _shell_quote(s: str) -> str:
  if re.fullmatch(r'[A-Za-z0-9_./:@=-]+', s):
    return s
  return "'" + s.replace("'", "'\\''") + "'"


def _prefetch_task(task_id: str) -> tuple[str, str]:
  """return (task_name, prompt_block) for a task ref.

  Fetches the task metadata + page body here, in dive-in, so the seeded `/fix`
  message can carry them and the agent's first turn doesn't call get_task_info /
  read_page_content. The flow MCP server is not yet connected on the session's
  first turn, so an in-session call would race the connection and error.
  """
  import dataclasses
  import enum
  import json

  from flow.system import default_system

  system = default_system()
  task = system.get_task_info(task_id)
  page = system.get_page_content(task_id)
  meta = json.dumps(
    dataclasses.asdict(task),
    default=lambda o: o.value if isinstance(o, enum.Enum) else str(o),
    indent=2,
  )
  block = (
    'Task metadata and page body were pre-fetched at launch (the flow MCP server '
    'is not yet connected on the first turn) — use them as your initial read; do '
    'not call get_task_info / read_page_content for this task.\n\n'
    f'## Task metadata\n```json\n{meta}\n```\n\n## Task page\n{page}'
  )
  return task.name, block


def _pick_fresh_name(base: str) -> str:
  """return base-<rand> — a unique workspace name; every launch gets a fresh one.

  The session commits to a worktree-<slug> branch. A random suffix makes that
  branch unique by construction, so a later session never reuses a slug whose
  remote branch still holds unmerged work — the remote needs no consulting.
  Only the local dirs are checked (no network); the loop just regenerates on the
  vanishingly rare clash with a live workspace.
  """
  project = cw._project_root()
  worktrees = cw._worktrees_dir(project)
  containers = cw._containers_dir(project)
  while True:
    name = f'{base}-{secrets.token_hex(4)}'
    if not (worktrees / name).exists() and not (containers / name).exists():
      return name


def _fix_command(task_arg: Optional[str], focus: bool, new: bool, command: Optional[str]) -> str:
  """build the `/fix …` first-user-message for a non-bare dive-in.

  Mirrors the CLI: task ref + optional `--focus`; or `--new` + optional seed +
  optional `--focus`; or bare `--focus`. The `/fix` skill body interprets the
  args.
  """
  parts = ['/fix']
  if new:
    parts.append('--new')
    if command is not None:
      parts.append(command)
    if focus:
      parts.append('--focus')
  elif task_arg is not None and focus:
    # focus was already set on the resolved task; use the focused form so the
    # skill body reads it back from get_focused_task.
    parts.append('--focus')
  elif task_arg is not None:
    parts.append(task_arg)
  else:
    parts.append('--focus')
  return ' '.join(parts)


def dive_in(
  forwarded: list[str],
  dry_run: bool = False,
  host: bool = False,
  command: Optional[str] = None,
  task: Optional[str] = None,
  new: bool = False,
  focus: bool = False,
) -> int:
  prompt: Optional[str] = None
  if new:
    base = _slugify(command) if command is not None else ''
    if len(base) == 0:
      base = 'dive-in-new'
    name = _pick_fresh_name(base)
    log.info('workspace: %s', name)
    prompt = _fix_command(task_arg=None, focus=focus, new=True, command=command)
  elif task is not None or focus:
    if task is not None:
      task_id = parse_page_ref(task)
      if focus:
        default_client().set_focus(task_id)
        log.info('focused task: %s', task_id)
    else:
      state = default_client().get_focus()
      if state is None:
        log.error('no task is currently focused')
        return 1
      task_id = state.task.id

    task_name, task_block = _prefetch_task(task_id)
    log.info('task: %s', task_name)
    prompt = _fix_command(task_arg=task, focus=focus, new=False, command=None)
    prompt = f'{prompt}\n\n{task_block}'
    if command is not None:
      prompt = f'{prompt}\n\nOnce you understand the task, {command}'

    base = _slugify(task_name)
    if len(base) == 0:
      base = 'dive-in'
    name = _pick_fresh_name(base)
    log.info('workspace: %s', name)

    os.environ['CW_TASK_ID'] = task_id
  else:
    prompt = command
    name = _pick_fresh_name('dive-in')
    log.info('workspace: %s', name)

  # surface ppp-dev's skills (/fix, /pr, /land): the in-place session runner
  # (cw/runner.py) reads CW_BRO, populates a per-session skills dir, and passes
  # it to claude via --add-dir.
  os.environ['CW_BRO'] = 'ppp-dev'

  ppp_parts = ['dive-in', *forwarded]
  if host:
    ppp_parts.append('--host')
  if new:
    ppp_parts.append('--new')
  if focus:
    ppp_parts.append('--focus')
  if task is not None:
    ppp_parts.extend(['-t', task])
  if command is not None:
    ppp_parts.append(command)
  os.environ.setdefault('PPP_SHELL_COMMAND', ' '.join(ppp_parts))

  # --mcp=http (joined form), not a bare --mcp: `cw ss --mcp` is nargs='?', so a bare
  # flag immediately followed by the positional name makes argparse consume the name as
  # its value. dive-in always wants the default http flow MCP.
  cw_command = ['cw', 'ss', '--mcp=http', *forwarded]
  if not host:
    cw_command.append('-c')
  if prompt is not None:
    cw_command.extend(['-p', prompt])
  cw_command.append(name)
  if dry_run:
    print(' '.join(_shell_quote(c) for c in cw_command))
    return 0
  return subprocess.run(cw_command).returncode


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='start a cw session focused on a task')
  parser.add_argument(
    '-n', '--dry-run', action='store_true', help='print the command without running it'
  )
  cw.add_forwarded_flags(parser)
  parser.add_argument(
    '--host',
    action='store_true',
    help='run on the host in a same-machine worktree instead of a container',
  )
  group = parser.add_mutually_exclusive_group()
  group.add_argument('-t', '--task', default=None, help='task ID or Notion URL to dive into')
  group.add_argument(
    '--new',
    action='store_true',
    help='start by creating a new task, then dive into it',
  )
  parser.add_argument(
    '--focus',
    action='store_true',
    help='dive into the currently focused task; with -t, set focus to that task first; with --new, focus the newly created task',
  )
  parser.add_argument(
    'command',
    nargs='?',
    default=None,
    help='initial command for the session (with no task flag, used as the entire prompt; with --new, used as the seed idea for the task; otherwise appended to the prompt)',
  )
  args = parser.parse(argv)
  forwarded = cw.extract_forwarded_argv(args)
  return dive_in(forwarded=forwarded, **args)
