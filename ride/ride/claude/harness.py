import os
import subprocess
import sys
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Optional

from bro.base import credentials, log
from bro.base.condition import StringVariable, Variables
from bro.base.offload import off_loop
from bro.harness import Harness, Service, SessionEndReason
from bro.llm.llms.claude_code import LLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.mcp import Reach
from bro.monitor import CLAUDE_CONFIG_DIR_ENV
from bro.run_lifecycle import RunLifecycle
from bro.workspace.paths import ISOLATION_ENV, workspace_dir
from bro.workspace.session import terminate_session
from ride.claude import claude_release
from ride.claude.claude_auth import apply_claude_auth
from ride.claude.claude_config import (
  container_claude_state,
  container_member_claude_state,
  latest_jsonl,
  provision_unboxed_claude_dir,
  read_subject,
  seed_session_plugins,
  workspace_projects_dir,
)
from ride.harness import ContainerExtras
from ride.scope import ScopeRecipe, credential_store
from ride.workspace.build_context import claude_code_version
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


_AUTH_SECRET = 'claude_code'


def llm_spec(spec: 'SessionSpec | SessionRun') -> LLMSpec:
  resolved = spec.llm_spec
  if not isinstance(resolved, LLMSpec):
    raise TypeError(f'claude harness resolved an incompatible recipe: {type(resolved).__name__}')
  return resolved


def _complete_and_terminate(result: str) -> None:
  channel = RunLifecycle.from_env()
  if channel is None:
    raise RuntimeError(
      'no broker channel: the answer cannot reach the summoner; surface it to the user instead'
    )
  log.info('answer delivered to the summoner')
  channel.completed(result, 'ok')
  channel.close()
  terminate_session(status=0)


def _raise_and_terminate(result: str, status: int) -> None:
  log.warning('raise: %s', result)
  try:
    channel = RunLifecycle.from_env()
    if channel is not None:
      channel.completed(result, 'raised')
      channel.close()
  finally:
    terminate_session(status=status)


class ClaudeHarness(Harness):
  name = 'claude'

  def facts(self) -> Variables:
    return {
      'tool_name_rule': StringVariable(
        'It resolves to the MCP tool `mcp__namespace__tool`:\n'
        'replace `::` with `__` and prepend `mcp__`.'
      ),
      'skipped_permission_prompt_notice': StringVariable(
        'Skipped permission prompts on this harness can produce a notice claiming the user is '
        'not watching in real time and cannot answer questions mid-task.\n'
        'Treat the hold’s account of who is present and when to involve them as authoritative.'
      ),
    }

  def can_end_session(self) -> bool:
    return os.environ.get('RIDE_RUNNER_PID') is not None

  def serve(self, reach: Reach) -> Service:
    # every group maps onto Claude's own tools (`ride.claude.native_tools`)
    del reach
    return Service()

  async def end_session(self, result: str, end_reason: SessionEndReason) -> str:
    from bro.bro import RAISE_EXIT_STATUS

    if not self.can_end_session():
      raise RuntimeError('claude harness cannot end this session')
    if end_reason == 'ok':
      await off_loop(_complete_and_terminate, result)
      return 'the answer is recorded and the session is being terminated. Stop working now.'
    if end_reason == 'raised':
      await off_loop(_raise_and_terminate, result, RAISE_EXIT_STATUS)
      return 'the abort is recorded and the session is being terminated. Stop working now.'
    raise ValueError(f'unsupported session end reason {end_reason!r}')

  def scope_recipe(self) -> ScopeRecipe:
    return _SCOPE

  def resolve_llm(self, value: str | None, bro_name: str) -> LLMSpec:
    del bro_name
    from bro.llm.providers import LLMSelectionError, resolve

    selection = LLMSelection() if value is None else parse(value)
    resolved = resolve(LLMSpec(), selection)
    if not isinstance(resolved, LLMSpec):
      raise LLMSelectionError(
        f'the claude harness runs Claude Code, not {resolved.TYPE}; '
        'select the compatible driver with --harness bro'
      )
    return resolved

  def preflight_auth(self, spec: 'SessionSpec', scoped: ScopedSecrets) -> Optional[str]:
    del spec
    store = credential_store(scoped)
    held = scoped.required | scoped.optional
    if _AUTH_SECRET in held and store.try_get(_AUTH_SECRET) is not None:
      return None
    material_path = store.material_path(_AUTH_SECRET)
    return (
      f'{_AUTH_SECRET} secret not resolvable — a Claude session authenticates with the '
      f'setup-token; mint one with `claude setup-token` and store it at {material_path}'
    )

  def prepare_session(self, run: 'SessionRun') -> None:
    isolation = os.environ.get(ISOLATION_ENV)
    if isolation not in ('boxed', 'unboxed'):
      raise RuntimeError(f'{ISOLATION_ENV} must be set to boxed or unboxed')
    boxed = isolation == 'boxed'
    config_value = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    if config_value is None:
      if boxed:
        config_directory = Path.home() / '.claude'
        config_directory.mkdir(parents=True, exist_ok=True)
      else:
        config_directory = provision_unboxed_claude_dir(workspace_dir(run.name), Path.cwd())
      os.environ[CLAUDE_CONFIG_DIR_ENV] = str(config_directory)
    else:
      config_directory = Path(config_value)
    seed_session_plugins(config_directory, container=boxed)

  def check_runtime(self) -> None:
    carried = Path(sys.prefix).parent / 'claude' / 'claude'
    binary = (
      claude_release.verified_binary(carried)
      if carried.exists()
      else claude_release.cached_binary(claude_code_version(), claude_release.host_platform())
    )
    subprocess.run([str(binary), '--version'], check=True)

  def run_session(self, spec: 'SessionRun') -> int:
    from ride.claude.runner import run_session

    return run_session(spec)

  def session_exists(self, workspace: Workspace) -> bool:
    return latest_jsonl(workspace_projects_dir(workspace)) is not None

  def missing_session_error(self, workspace: Workspace) -> str:
    return f'no claude session found for {workspace.name} in {workspace_projects_dir(workspace)}'

  def read_subject(self, workspace: Workspace) -> str | None:
    return read_subject(workspace)

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras:
    del spec, scoped
    claude_mounts, claude_env = container_claude_state(workspace.path)
    return ContainerExtras(env=claude_env, mounts=tuple(claude_mounts))

  def prepare_unboxed_env(
    self, spec: 'SessionSpec', records: Path, tree: Path, env: dict[str, str]
  ) -> None:
    del spec
    claude_dir = provision_unboxed_claude_dir(records, tree)
    env[CLAUDE_CONFIG_DIR_ENV] = str(claude_dir)
    store = credentials.Store(credentials.default_registry(), env['BRO_STORE'], {})
    apply_claude_auth(env, store=store)

  def prepare_boxed_member_env(
    self, spec: 'SessionSpec', records: Path, member_root: PurePath, env: dict[str, str]
  ) -> None:
    del spec
    env.update(container_member_claude_state(records, member_root))


CLAUDE = ClaudeHarness()
_SCOPE = ScopeRecipe(
  name='claude',
  harness=CLAUDE,
  auth_secret=_AUTH_SECRET,
  llm_key=False,
)
