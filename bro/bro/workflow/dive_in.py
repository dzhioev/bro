#!/usr/bin/env python
"""start a cw session focused on a task."""

import os
import re
import secrets
import subprocess
from typing import Optional

import brog.model
import brog.system
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


def _is_flow_backend(system: brog.system.System) -> bool:
  import brog.flow_proxy

  return isinstance(system, brog.flow_proxy.System)


def _prefetch_task(system: brog.system.System, task_ref: str) -> tuple[brog.model.Task, str]:
  """return (task, prompt_block) for a task ref.

  Resolves the ref and fetches the task metadata + description + comments here,
  in dive-in, so the seeded `/fix` message can carry them and the agent's first
  turn doesn't call get_task / read_task / read_comments. The brog MCP server is
  not yet connected on the session's first turn, so an in-session call would
  race the connection and error.
  """
  import dataclasses
  import json

  task = system.get_task(task_ref)
  description = system.get_task_description(task.id)
  comments = system.get_task_comments(task.id)
  meta = json.dumps(dataclasses.asdict(task), indent=2)
  # Comment timestamps are datetimes; everything else is JSON-native
  comments_json = json.dumps(
    [dataclasses.asdict(comment) for comment in comments], default=str, indent=2
  )
  block = (
    'Task metadata, description, and comments were pre-fetched at launch (the '
    'brog MCP server is not yet connected on the first turn) — use them as your '
    'initial read; do not call get_task / read_task / read_comments for this '
    'task.\n\n'
    f'## Task metadata\n```json\n{meta}\n```\n\n'
    f'## Task description\n{description}\n\n'
    f'## Task comments\n```json\n{comments_json}\n```'
  )
  return task, block


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


def dive_in(
  forwarded: list[str],
  dry_run: bool = False,
  command: Optional[str] = None,
  task: Optional[str] = None,
  new: bool = False,
  focus: bool = False,
) -> int:
  """launch the session. session shaping — the persona (prompt, skills, MCP
  namespaces; ppp-dev by default) or the `--bro` flavor — rides the forwarded
  flags; dive-in adds nothing of its own."""
  prompt: Optional[str] = None
  if new:
    base = _slugify(command) if command is not None else ''
    if len(base) == 0:
      base = 'dive-in-new'
    name = _pick_fresh_name(base)
    log.info('workspace: %s', name)
    prompt = '/fix --new' if command is None else f'/fix --new {command}'
  elif task is not None or focus:
    system = brog.system.default_system()
    if focus and not _is_flow_backend(system):
      log.error('--focus requires the flow brog backend (the focus service stores flow task ids)')
      return 1
    if task is not None:
      task_ref = task
    else:
      state = default_client().get_focus()
      if state is None:
        log.error('no task is currently focused')
        return 1
      task_ref = state.task.id

    brog_task, task_block = _prefetch_task(system, task_ref)
    if task is not None and focus:
      default_client().set_focus(brog_task.id)
      log.info('focused task: %s', brog_task.id)
    log.info('task: %s', brog_task.name)
    # the ref exactly as given — the prefetch block carries the canonical form
    prompt = f'/fix {task_ref}\n\n{task_block}'
    if command is not None:
      prompt = f'{prompt}\n\nOnce you understand the task, {command}'

    base = _slugify(brog_task.name)
    if len(base) == 0:
      base = 'dive-in'
    name = _pick_fresh_name(base)
    log.info('workspace: %s', name)

    os.environ['CW_TASK_ID'] = brog_task.id
  else:
    prompt = command
    name = _pick_fresh_name('dive-in')
    log.info('workspace: %s', name)

  cw_command = ['cw', 'ss', *forwarded]
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
  group = parser.add_mutually_exclusive_group()
  group.add_argument('-t', '--task', default=None, help='task id, URL, or issue ref to dive into')
  group.add_argument(
    '--new',
    action='store_true',
    help='start by creating a new task, then dive into it',
  )
  parser.add_argument(
    '--focus',
    action='store_true',
    help='dive into the currently focused task; with -t, set focus to that task first (flow backend only)',
  )
  # focus cannot attach to a task that does not exist yet at launch
  parser.add_exclusive_groups(['new'], ['focus'])
  parser.add_argument(
    'command',
    nargs='?',
    default=None,
    help='initial command for the session (with no task flag, used as the entire prompt; with --new, used as the seed idea for the task; otherwise appended to the prompt)',
  )
  args = parser.parse(argv)
  # the user-facing launch reconstruction, consumed by `cw banner` as
  # launch_command: built from dive-in's own parser so env-detection sees
  # `dive-in …`, not the underlying `cw ss`; setdefault keeps an outer wrapper's
  # value.
  os.environ.setdefault(
    'PPP_SHELL_COMMAND',
    ' '.join(parser.reconstruct(args, prog=['dive-in'], exclude=('dry_run',))),
  )
  forwarded = cw.extract_forwarded_argv(args)
  return dive_in(forwarded=forwarded, **args)
