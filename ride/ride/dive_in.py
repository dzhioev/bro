#!/usr/bin/env python
"""start a ride session focused on a task."""

import os
import re
import subprocess
from typing import Optional

import bro.brog.model as brog_model
import bro.brog.system as brog_system
from bro.base import credentials, log
from bro.base.args import Parser
from bro.launch.scope import LaunchScopeError, launch_view_store, scoped_secrets
from bro.workspace.git import fetch_ref
from bro.workspace.paths import fresh_workspace_name, project_root
from bro.workspace.project import project_config
from ride.cli import reports_location_errors
from ride.flags import add_forwarded_flags, extract_forwarded_argv, pop_harness_options
from ride.harness import get_harness

__cli_name__ = 'dive-in'


def _slugify(name: str) -> str:
  s = name.lower().strip()
  s = re.sub(r'[^a-z0-9]+', '-', s)
  s = s.strip('-')
  return s[:40].rstrip('-') if len(s) > 40 else s


def _shell_quote(s: str) -> str:
  if re.fullmatch(r'[A-Za-z0-9_./:@=-]+', s):
    return s
  return "'" + s.replace("'", "'\\''") + "'"


def _fresh_origin_head() -> Optional[str]:
  """origin's default-branch tip, freshly fetched; None when origin is unreachable."""
  return fetch_ref(project_root(), 'HEAD')


def _prefetch_task(system: brog_system.System, task_ref: str) -> tuple[brog_model.Task, str]:
  """return (task, prompt_block) for a task ref.

  Resolves the ref and fetches the task metadata + description + comments here,
  in dive-in, so the seeded `[[fix …]]` message can carry them and the agent's first
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


def _task_system(
  grant: list[str], revoke: list[str], bro: Optional[str], harness: str, harness_options: dict
) -> brog_system.System:
  """the brog backend for the task prefetch, reading `brog` through the launch's
  own credential binding (`launch_view_store`) — so `--grant`/`--revoke` select
  the same brog config the session's store hydrates."""
  project = project_config()
  bro_name = bro if bro is not None else project.default_bro
  store = launch_view_store(
    scoped_secrets(bro_name, get_harness(harness).scope_recipe(harness_options)),
    grant=grant,
    revoke=revoke,
  )
  return brog_system.build_system(lambda: store.get_json('brog'))


def dive_in(
  forwarded: list[str],
  dry_run: bool = False,
  command: Optional[str] = None,
  task: Optional[str] = None,
  new: bool = False,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  bro: Optional[str] = None,
  harness: str = 'claude',
  harness_options: Optional[dict] = None,
) -> int:
  """launch the session. session shaping — the bro (prompt, spells, MCP
  namespaces) selected by `--bro` or the project default, the harness, or the
  claude `--raw` flavor — rides the forwarded flags; dive-in adds nothing of
  its own beyond binding the task prefetch to the same scope (`_task_system`)."""
  prompt: Optional[str] = None
  if new:
    base = _slugify(command) if command is not None else ''
    if len(base) == 0:
      base = 'dive-in-new'
    name = fresh_workspace_name(base)
    log.info('workspace: %s', name)
    prompt = '[[fix --new ""]]' if command is None else f'[[fix --new {command}]]'
  elif task is not None:
    try:
      system = _task_system(
        grant or [],
        revoke or [],
        bro,
        harness,
        harness_options if harness_options is not None else {},
      )
    except (LaunchScopeError, credentials.SecretNotFound, ValueError) as error:
      log.error('cannot open the task tracker for the prefetch: %s', error)
      return 1
    task_ref = task
    brog_task, task_block = _prefetch_task(system, task_ref)
    log.info('task: %s', brog_task.name)
    # the ref exactly as given — the prefetch block carries the canonical form
    prompt = f'[[fix {task_ref}]]\n\n{task_block}'
    if command is not None:
      prompt = f'{prompt}\n\nOnce you understand the task, {command}'

    base = _slugify(brog_task.name)
    if len(base) == 0:
      base = 'dive-in'
    name = fresh_workspace_name(base)
    log.info('workspace: %s', name)

    os.environ['RIDE_TASK_ID'] = brog_task.id
  else:
    prompt = command
    name = fresh_workspace_name('dive-in')
    log.info('workspace: %s', name)

  bro_name = bro if bro is not None else project_config().default_bro
  ride_command = ['ride', 'along', *forwarded, '--workspace', name, bro_name]
  if prompt is not None:
    ride_command.append(prompt)
  if dry_run:
    print(' '.join(_shell_quote(value) for value in ride_command))
    return 0
  return subprocess.run(ride_command).returncode


@reports_location_errors
def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='start a ride session focused on a task')
  parser.add_argument(
    '-n', '--dry-run', action='store_true', help='print the command without running it'
  )
  add_forwarded_flags(parser)
  group = parser.add_mutually_exclusive_group()
  group.add_argument('-t', '--task', default=None, help='task id, URL, or issue ref to dive into')
  group.add_argument(
    '--new',
    action='store_true',
    help='start by creating a new task, then dive into it',
  )
  parser.add_argument(
    'command',
    nargs='?',
    default=None,
    help='initial command for the session (with no task flag, used as the entire prompt; with --new, used as the seed idea for the task; otherwise appended to the prompt)',
  )
  args = parser.parse(argv)
  os.environ.setdefault(
    'BRO_SHELL_COMMAND',
    ' '.join(parser.reconstruct(args, prog=['dive-in'], exclude=('dry_run',))),
  )
  if args['into'] is None:
    base_ref = _fresh_origin_head()
    if base_ref is None:
      log.warning('cannot fetch origin HEAD; basing the session on the host checkout HEAD')
    else:
      log.info('base: origin HEAD %s', base_ref[:12])
      args['into'] = base_ref
  # the prefetch binds to the same scope the session launches with, so the
  # scope-shaping flags are read here as well as forwarded
  harness_name = args['harness'] or project_config().harness
  harness_options = pop_harness_options(
    parser, dict(args), harness_name, solo=False, host=args['host']
  )
  scope_args = {key: args[key] for key in ('grant', 'revoke', 'bro')}
  args['bro'] = None
  forwarded = extract_forwarded_argv(args)
  return dive_in(
    forwarded=forwarded,
    harness=harness_name,
    harness_options=harness_options,
    **scope_args,
    **args,
  )
