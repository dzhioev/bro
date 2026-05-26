#!/usr/bin/env python
"""start a cw session focused on a task."""

import os
import re
import subprocess
import sys

import cw
from base import log
from base.args import Parser
from flow.focus.client.client import default_client
from prompts import get_prompt


def _slugify(name: str) -> str:
  s = name.lower().strip()
  s = re.sub(r'[^a-z0-9]+', '-', s)
  s = s.strip('-')
  return s[:40].rstrip('-') if len(s) > 40 else s


def _shell_quote(s: str) -> str:
  if re.fullmatch(r'[A-Za-z0-9_./:@=-]+', s):
    return s
  return "'" + s.replace("'", "'\\''") + "'"


_NOTION_URL_RE = re.compile(r'https?://(?:www\.)?notion\.(?:so|site)/.+-([0-9a-f]{32})(?:\?.*)?$')
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
  """return base, or base-2/-3/... — the first slug with no existing worktree/container dir.

  Both namespaces (host worktrees and container sessions) are checked together so a
  --new dive-in never lands on a directory already in use by either mode.
  """
  proj = cw._project_root()
  worktrees = proj / '.claude' / 'worktrees'
  containers = proj / 'var' / 'cw' / 'containers'
  i = 1
  while True:
    name = base if i == 1 else f'{base}-{i}'
    if not (worktrees / name).exists() and not (containers / name).exists():
      return name
    i += 1


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
    hint = f'- Initial idea from the user: {command}\n' if command is not None else ''
    prompt = get_prompt(
      'dive_in_new.prompt.template', hint=hint, context=get_prompt('dive_in_context.prompt')
    )
    if focus:
      prompt = (
        f'{prompt}\n\nAfter creating the task, call set_focus on its id so it becomes the '
        'currently focused task.'
      )
    name = _slugify(command) if command is not None else ''
    if len(name) == 0:
      name = 'dive-in-new'
    fresh = _pick_fresh_name(name)
    if fresh != name:
      log.info('workspace %s is in use, picking %s', name, fresh)
    name = fresh
  elif task is not None or focus:
    if task is not None:
      task_id = _resolve_task_id(task)
      if focus:
        default_client().set_focus(task_id)
        log.info('focused task: %s', task_id)
      task_name = _resolve_task_name(task_id)
      log.info('task: %s', task_name)
      if not resume:
        if focus:
          startup = get_prompt('dive_in_focused.prompt')
          target = 'the currently focused task'
        else:
          startup = get_prompt('dive_in_task.prompt.template', task_id=task_id)
          target = f'task {task_id}'
        prompt = get_prompt(
          'dive_in.prompt.template',
          target=target,
          startup=startup,
          context=get_prompt('dive_in_context.prompt'),
        )
    else:
      state = default_client().get_focus()
      if state is None:
        log.error('no task is currently focused')
        return 1
      task_id = state.task.id
      task_name = state.task.name
      log.info('focused: %s', task_name)
      if not resume:
        prompt = get_prompt(
          'dive_in.prompt.template',
          target='the currently focused task',
          startup=get_prompt('dive_in_focused.prompt'),
          context=get_prompt('dive_in_context.prompt'),
        )

    name = _slugify(task_name)
    if len(name) == 0:
      name = 'dive-in'

    if not resume and command is not None:
      prompt = f'{prompt}\n\nOnce you understand the task, {command}'

    os.environ['CW_TASK_ID'] = task_id
  else:
    prompt = command
    name = _pick_fresh_name('dive-in')

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
