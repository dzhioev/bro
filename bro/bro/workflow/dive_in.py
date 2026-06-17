#!/usr/bin/env python
"""start a cw session focused on a task."""

import os
import re
import secrets
import subprocess
import sys

import cw
from base import log
from base.args import Parser
from flow.focus.client.client import default_client


def _slugify(name: str) -> str:
  s = name.lower().strip()
  s = re.sub(r'[^a-z0-9]+', '-', s)
  s = s.strip('-')
  return s[:40].rstrip('-') if len(s) > 40 else s


def _shell_quote(s: str) -> str:
  if re.fullmatch(r'[A-Za-z0-9_./:@=-]+', s):
    return s
  return "'" + s.replace("'", "'\\''") + "'"


_NOTION_URL_RE = re.compile(
  r'https?://(?:[\w-]+\.)?notion\.(?:so|site|com)/(?:[^?\s]*[/-])?([0-9a-f]{32})(?:\?.*)?$'
)
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _resolve_task_id(task_ref: str) -> str:
  task_ref = task_ref.replace('\\', '')
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


def _pick_fresh_name(base: str) -> str:
  """return base-<rand> — a unique slug for a --new or bare dive-in.

  The session commits to a worktree-<slug> branch. A random suffix makes that
  branch unique by construction, so a later session never reuses a slug whose
  remote branch still holds unmerged work — the remote needs no consulting.
  Only the local dirs are checked (no network); the loop just regenerates on the
  vanishingly rare clash with a live workspace.
  """
  proj = cw._project_root()
  worktrees = proj / '.claude' / 'worktrees'
  containers = proj / 'var' / 'cw' / 'containers'
  while True:
    name = f'{base}-{secrets.token_hex(3)}'
    if not (worktrees / name).exists() and not (containers / name).exists():
      return name


def _fix_command(task_arg: str | None, focus: bool, new: bool, command: str | None) -> str:
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
  command: str | None = None,
  task: str | None = None,
  new: bool = False,
  focus: bool = False,
  resume: bool = False,
) -> int:
  prompt: str | None = None
  if new:
    base = _slugify(command) if command is not None else ''
    if len(base) == 0:
      base = 'dive-in-new'
    name = _pick_fresh_name(base)
    log.info('workspace: %s', name)
    prompt = _fix_command(task_arg=None, focus=focus, new=True, command=command)
  elif task is not None or focus:
    if task is not None:
      task_id = _resolve_task_id(task)
      if focus:
        default_client().set_focus(task_id)
        log.info('focused task: %s', task_id)
      task_name = _resolve_task_name(task_id)
      log.info('task: %s', task_name)
    else:
      state = default_client().get_focus()
      if state is None:
        log.error('no task is currently focused')
        return 1
      task_id = state.task.id
      task_name = state.task.name
      log.info('focused: %s', task_name)

    name = _slugify(task_name)
    if len(name) == 0:
      name = 'dive-in'

    if not resume:
      prompt = _fix_command(task_arg=task, focus=focus, new=False, command=None)
      if command is not None:
        prompt = f'{prompt}\n\nOnce you understand the task, {command}'

    os.environ['CW_TASK_ID'] = task_id
  else:
    prompt = command
    name = _pick_fresh_name('dive-in')
    log.info('workspace: %s', name)

  # surface ppp-dev's skills (/fix, /pr, /land) via .claude/skills/ symlinks.
  # in container mode the entrypoint reads CW_BRO and runs `cw populate-bro-skills`;
  # in host mode .claude/hooks/session_start.sh does the same.
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

  cmd = ['cw', 'ss', '--mcp', *forwarded]
  if not host:
    cmd.append('-c')
  if prompt is not None:
    cmd.extend(['-p', prompt])
  cmd.append(name)
  if dry_run:
    print(' '.join(_shell_quote(c) for c in cmd))
    return 0
  return subprocess.run(cmd).returncode


def main(argv=None):
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
  if args['resume']:
    if args['new']:
      parser.error('--resume cannot be combined with --new')
    if args['task'] is None and not args['focus']:
      parser.error('--resume requires a task — pass -t <task> or --focus')
    if args['command'] is not None:
      parser.error(
        '--resume cannot be combined with a positional command (it is ignored on resume)'
      )
  resume = args['resume']
  forwarded = cw.extract_forwarded_argv(args)
  return dive_in(forwarded=forwarded, resume=resume, **args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
