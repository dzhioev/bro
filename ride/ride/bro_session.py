import json
import os
import subprocess
from typing import Optional

from bro.base import credentials, log
from bro.launch.bro_run import describe
from bro.launch.broxy import START_SESSION_BROXY_ENV
from bro.launch.identity import bro_git_identity_env
from bro.launch.root import run_host_process_via_broker, run_in_container
from bro.llm.llm import NativeLLMSpec
from bro.monitor import trail_pointer
from bro.workspace.containers import broker_enabled
from bro.workspace.docker import find_container_id
from bro.workspace.model import Workspace
from bro.workspace.paths import project_root, venv_env
from bro.workspace.store import log_scoped_secrets, materialize_scoped_store
from bro.workspace.worktrees import ensure_host_worktree, provision_host_worktree
from ride.bro import options
from ride.session import ScopedLaunch, SessionSpec


def _resume_trail(workspace: Workspace, spec: SessionSpec) -> Optional[str]:
  if not spec.resume:
    return None
  trail_id = trail_pointer.read(workspace.path / trail_pointer.FILENAME)
  if trail_id is None:
    raise ValueError(
      f'cannot resume bro harness workspace {workspace.name!r}: no trail pointer was published; '
      'the session may have run without a broker or with --no-trails'
    )
  return trail_id


def _inner_arguments(spec: SessionSpec, resume_trail: Optional[str]) -> list[str]:
  bro = options(spec)
  arguments: list[str] = []
  if spec.prompt is not None:
    arguments.append(spec.prompt)
  if spec.solo and bro.rich:
    arguments.append('--rich')
  if not spec.solo and bro.text:
    arguments.append('--text')
  if spec.llm is not None:
    arguments.extend(['--llm', spec.llm])
  arguments.extend(['--hold', spec.hold])
  if resume_trail is not None:
    resolved = spec.llm_spec
    if not isinstance(resolved, NativeLLMSpec):
      raise ValueError(
        f'bro harness resume requires a native recipe, not {type(resolved).__name__}'
      )
    arguments.extend(
      [
        '--continue-trail',
        resume_trail,
        '--continue-llm',
        json.dumps(resolved.dump(), separators=(',', ':')),
      ]
    )
  return arguments


def inner_command(spec: SessionSpec) -> list[str]:
  from ride.bro import session_trail_pointer

  resume_trail = trail_pointer.read(session_trail_pointer(spec.name)) if spec.resume else None
  if spec.resume and resume_trail is None:
    raise ValueError(f'no bro harness trail recorded for workspace {spec.name!r}')
  verb = 'run' if spec.solo else 'chat'
  return ['bro', verb, spec.bro, *_inner_arguments(spec, resume_trail), '--in-place']


def launch_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  *,
  container: bool,
) -> int:
  if not spec.resume:
    trail_pointer.clear(workspace.path / trail_pointer.FILENAME)
  try:
    resume_trail = _resume_trail(workspace, spec)
  except ValueError as error:
    log.error('%s', error)
    return 1
  if container:
    return _container_session(spec, workspace, base_ref, launch_scope, resume_trail)
  return _host_session(spec, workspace, base_ref, launch_scope, resume_trail)


def _container_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  resume_trail: Optional[str],
) -> int:
  if find_container_id(workspace.tree) is not None:
    log.error(
      'session already active in the container for workspace %r; refusing to start a second',
      spec.name,
    )
    return 1
  launch = describe(
    spec.bro,
    _inner_arguments(spec, resume_trail),
    workspace_name=spec.name,
    verb='run' if spec.solo else 'chat',
    scoped=launch_scope.scoped,
    base_ref=base_ref,
    trails=not options(spec).no_trails,
    tty=not spec.solo,
  )
  return run_in_container(
    launch,
    workspace=workspace,
    may_summon=launch_scope.may_summon,
    trail_pointer=workspace.path / trail_pointer.FILENAME,
  )


def _host_session(
  spec: SessionSpec,
  workspace: Workspace,
  base_ref: Optional[str],
  launch_scope: ScopedLaunch,
  resume_trail: Optional[str],
) -> int:
  project = project_root()
  os.chdir(project)
  worktree = workspace.tree
  scoped = launch_scope.scoped
  log_scoped_secrets(spec.name, scoped.required, scoped.optional)
  if not ensure_host_worktree(worktree, workspace.metadata.branch, base_ref):
    return 1
  if not provision_host_worktree(worktree):
    return 1

  bro_binary = worktree / '.venv' / 'bin' / 'bro'
  if not bro_binary.is_file():
    log.error(
      'no bro in %s — the worktree base does not provide the bro in-place runner',
      bro_binary,
    )
    return 1
  command = [
    str(bro_binary),
    'run' if spec.solo else 'chat',
    spec.bro,
    *_inner_arguments(spec, resume_trail),
    '--in-place',
  ]
  runner_env = venv_env(worktree / '.venv')
  runner_env.update(bro_git_identity_env(spec.bro))
  runner_env['RIDE_BRO'] = spec.bro
  runner_env[START_SESSION_BROXY_ENV] = '1'
  runner_env[credentials.REGISTRY_ENV] = str(
    materialize_scoped_store(launch_scope.store, workspace.path / '.bro')
  )
  if options(spec).no_trails:
    runner_env['TRAILS_DISABLED'] = '1'

  workspace.clear_session_end()
  if broker_enabled():
    code = run_host_process_via_broker(
      workspace,
      command,
      runner_env,
      launch_scope.may_summon,
      scoped.required | scoped.optional,
      interactive=not spec.solo,
      trail_pointer=workspace.path / trail_pointer.FILENAME,
    )
  else:
    runner_env.pop(START_SESSION_BROXY_ENV, None)
    runner_env.pop('BROKER_CHANNEL', None)
    code = subprocess.run(command, cwd=str(worktree), env=runner_env).returncode
  workspace.record_session_end(code)
  return code
