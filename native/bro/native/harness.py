import asyncio
import json
import math
import subprocess
import time
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Literal, Optional, Protocol, cast

import bro.llm.mcp as llm_mcp
from bro import spells as spell_store
from bro.base import log, spawn
from bro.base.condition import StringVariable, Variables
from bro.base.offload import off_loop
from bro.base.text_window import DEFAULT_LIMIT
from bro.harness import Harness, Service, SessionEndReason
from bro.inbox import Inbox
from bro.jobs import Job, JobStatus, Registry
from bro.launch.llm_flags import resolve_native
from bro.llm.llm import NativeLLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import trail_pointer
from bro.native import dev_mcp
from ride.harness import ContainerExtras
from ride.scope import ScopeRecipe
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.bro import BaseBro, LiveRun
  from bro.mcp import Reach
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


class _NativeRun(Protocol):
  @property
  def inbox(self) -> Inbox: ...

  @property
  def registry(self) -> Registry: ...

  @property
  def brash_policy(self) -> Optional[Path]: ...


_JOB_WAIT_CAP_SECONDS = 3600.0
_FOREGROUND_WAIT_SECONDS = 45.0

_JOB_DESCRIPTION = (
  'start a shell command line as one supervised job (merged stdout and stderr, continuously '
  'spooled output). under this persona’s finite command list the line runs in brash, which starts '
  'only the commands the list admits and stops at the first it refuses, exiting 126 with the '
  'refusal in the output; unrestricted personas run it in `bash -c`. `fg` waits for exit and '
  'returns tail-kept output; if its timeout or other job news ends the wait first, the job becomes '
  '`bg` and the result names its id and `poll` continuation. `bg` returns immediately and reports '
  'only its exit through this run’s notifications. `timeout_seconds` is capped at '
  f'{_JOB_WAIT_CAP_SECONDS:g} and a clamp is named in the result. output is bounded by `limit` '
  'lines and the shared byte cap, with skipped/pending markers.'
)

_POLL_DESCRIPTION = (
  'read currently unread output from a job without blocking. the default is a head read: oldest '
  'lines first, advancing only past what it returned and leaving the rest behind a pending '
  'marker. `tail=true` jumps to the end, keeps the last `limit` lines, and announces the skipped '
  'middle, which is not delivered later. every result starts with `running` or '
  '`exited (code N)`. output is bounded by `limit` lines and the shared byte cap.'
)

_KILL_DESCRIPTION = (
  'terminate a job’s whole supervised process group with SIGTERM, escalating to SIGKILL after '
  'the grace period. waits for and consumes the exit it forces, so no later exit notification '
  'follows; unread output remains available to `poll`, and unread watch output remains eligible '
  'for notification delivery.'
)

_JOBS_DESCRIPTION = (
  'list every job in this service registry with its id, mode, command, running/exited state, '
  'exit code, and count of unread output lines.'
)


def _bounded_wait(seconds: float, field: str) -> tuple[float, Optional[str]]:
  if not math.isfinite(seconds) or seconds <= 0:
    raise ValueError(f'{field} must be a finite positive number')
  if seconds <= _JOB_WAIT_CAP_SECONDS:
    return seconds, None
  return _JOB_WAIT_CAP_SECONDS, f'{field} {seconds:g} clamped to {_JOB_WAIT_CAP_SECONDS:g}'


async def _wait_for_foreground_job(
  job: Job,
  *,
  inbox: Inbox,
  timeout_seconds: float,
  limit: int,
) -> str:
  wait_seconds, clamp_note = _bounded_wait(timeout_seconds, 'timeout_seconds')
  deadline = time.monotonic() + wait_seconds
  try:
    with inbox.waiter() as cancelled:
      await off_loop(inbox.wait, deadline, cancelled)
  except asyncio.CancelledError:
    job.become_background()
    raise

  result, became_background = job.settle_foreground(limit)
  if became_background:
    result = f'{result}\n{job.id} continues in bg mode; read on with poll(id={job.id!r})'
  if clamp_note is not None:
    result = f'{result}\n[{clamp_note}]'
  return result


def _job_tools(live_run: _NativeRun) -> list[llm_mcp.Tool]:
  def start(command: str, mode: str) -> Job:
    if len(command.strip()) == 0:
      raise ValueError('command must be non-empty')
    return live_run.registry.start(command, mode, live_run.brash_policy)  # type: ignore[arg-type]

  async def job(
    command: str,
    mode: Literal['fg', 'bg'] = 'fg',
    timeout_seconds: float = _FOREGROUND_WAIT_SECONDS,
    limit: int = DEFAULT_LIMIT,
  ) -> str:
    started = start(command, mode)
    if mode != 'fg':
      return f'started {started.id} ({mode})'
    return await _wait_for_foreground_job(
      started, inbox=live_run.inbox, timeout_seconds=timeout_seconds, limit=limit
    )

  def poll(id: str, limit: int = DEFAULT_LIMIT, tail: bool = False) -> str:
    return live_run.registry.get(id).poll(limit, tail=tail)

  async def kill(id: str) -> str:
    target = live_run.registry.get(id)
    return await off_loop(target.kill)

  def jobs() -> list[JobStatus]:
    return [entry.status() for entry in live_run.registry.values()]

  return [
    llm_mcp.FunctionTool(job, name='job', description=_JOB_DESCRIPTION),
    llm_mcp.FunctionTool(poll, name='poll', description=_POLL_DESCRIPTION),
    llm_mcp.FunctionTool(kill, name='kill', description=_KILL_DESCRIPTION),
    llm_mcp.FunctionTool(jobs, name='jobs', description=_JOBS_DESCRIPTION),
  ]


def _session_options(spec: 'SessionSpec | SessionRun', resume_trail: Optional[str]) -> list[str]:
  options: list[str] = []
  if spec.llm is not None:
    options.extend(['--llm', spec.llm])
  options.extend(['--hold', spec.hold])
  if resume_trail is not None:
    resolved = spec.llm_spec
    if not isinstance(resolved, NativeLLMSpec):
      raise ValueError(
        f'bro harness resume requires a native recipe, not {type(resolved).__name__}'
      )
    options.extend(
      [
        '--continue-trail',
        resume_trail,
        '--continue-llm',
        json.dumps(resolved.dump(), separators=(',', ':')),
      ]
    )
  return options


class BroHarness(Harness):
  name = 'bro'

  def facts(self) -> Variables:
    return {
      'tool_name_rule': StringVariable(
        'In your tool list it is `namespace__tool`:\n'
        'replace `::` with `__` and call that wire name directly.'
      ),
      'skipped_permission_prompt_notice': StringVariable(''),
    }

  def prompt_instructions(self) -> str:
    return '\n'.join(
      [
        '## Skills',
        '',
        'Third-party skills load through `bro::skill`. A user message starting with `/<name>` '
        'requests that skill: call `bro::skill` with its name, then execute the returned '
        'instructions with the rest of the message as arguments. An empty body means the skill '
        'is unavailable.',
      ]
    )

  def can_end_session(self) -> bool:
    return True

  def serve(self, reach: 'Reach') -> Service:
    server_specs = ()
    if reach.files is not None:
      tool_names = () if reach.files.write else dev_mcp.READ_ONLY
      server_specs = (dev_mcp.toolset.manifest(*tool_names),)
    unserved = tuple(group.key.name for group in (reach.web, reach.delegation) if group is not None)
    return Service(server_specs=server_specs, unserved=unserved)

  def own_tools(self, bro: 'BaseBro', live_run: 'LiveRun | None') -> tuple[llm_mcp.Tool, ...]:
    tools = [spell_store.build_skill_tool()]
    if bro.reach().brash is None:
      return tuple(tools)
    if live_run is None:
      raise RuntimeError('job tools require a live run')
    tools.extend(_job_tools(cast(_NativeRun, live_run)))
    return tuple(tools)

  async def end_session(self, result: str, end_reason: SessionEndReason) -> str:
    from bro.bro import AnswerDelivered, BroRaised

    if end_reason == 'ok':
      raise AnswerDelivered(result)
    if end_reason == 'raised':
      raise BroRaised(result)
    raise ValueError(f'unsupported session end reason {end_reason!r}')

  def scope_recipe(self) -> ScopeRecipe:
    return _BRO_RUN_RECIPE

  def resolve_llm(self, value: str | None, bro_name: str) -> NativeLLMSpec:
    from bro.registry import get_class

    selection = LLMSelection() if value is None else parse(value)
    return resolve_native(get_class(bro_name).llm_spec, selection)

  def preflight_auth(self, spec: 'SessionSpec', scoped: ScopedSecrets) -> Optional[str]:
    del spec, scoped
    return None

  def session_exists(self, workspace: Workspace) -> bool:
    return trail_pointer.read(trail_pointer.session_pointer(workspace.path)) is not None

  def missing_session_error(self, workspace: Workspace) -> str:
    return (
      f'cannot resume bro harness workspace {workspace.name!r}: no trail pointer was published; '
      'the session may have run without a broker or with --no-trails'
    )

  def read_subject(self, workspace: Workspace) -> str | None:
    from ride.session import load_resume_spec

    spec = load_resume_spec(workspace)
    return None if spec is None else spec.subject

  def prepare_session(self, run: 'SessionRun') -> None:
    del run

  def clean_cache(self, *, dry_run: bool = False) -> None:
    del dry_run

  def check_runtime(self) -> None:
    subprocess.run([spawn.console_script('bro'), '--help'], check=True)

  def run_session(self, spec: 'SessionSpec | SessionRun') -> int:
    from ride.do_ride import run_agent

    try:
      executable = spawn.console_script('bro')
    except FileNotFoundError as error:
      log.error('%s', error)
      return 1
    resume_trail: Optional[str] = None
    if spec.resume:
      pointer = trail_pointer.path()
      resume_trail = trail_pointer.read(pointer) if pointer is not None else None
      if resume_trail is None:
        log.error('no bro harness trail recorded for workspace %s', spec.name)
        return 1
    verb = 'run' if spec.solo else 'chat'
    argv = [
      executable,
      verb,
      *_session_options(spec, resume_trail),
      '--',
      spec.bro,
      *([] if spec.prompt is None else [spec.prompt]),
    ]
    return run_agent(argv)

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras:
    del spec, workspace, scoped
    return ContainerExtras(env={}, mounts=())

  def prepare_unboxed_env(
    self, spec: 'SessionSpec', records: Path, tree: Path, env: dict[str, str]
  ) -> None:
    del spec, records, tree, env

  def prepare_boxed_member_env(
    self, spec: 'SessionSpec', records: Path, member_root: PurePath, env: dict[str, str]
  ) -> None:
    del spec, records, member_root, env


BRO = BroHarness()
_BRO_RUN_RECIPE = ScopeRecipe(
  name='bro-run',
  harness=BRO,
  auth_secret=None,
  llm_key=True,
)
