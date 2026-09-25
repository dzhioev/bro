from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Optional

from bro.base import credentials
from bro.llm.llms.claude_code import LLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import CLAUDE_CONFIG_DIR_ENV
from ride.claude.claude_auth import apply_claude_auth
from ride.claude.claude_config import (
  container_claude_state,
  container_member_claude_state,
  latest_jsonl,
  provision_unboxed_claude_dir,
  read_subject,
  workspace_projects_dir,
)
from ride.harness import ContainerExtras
from ride.scope import ScopeRecipe, credential_store
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


_AUTH_SECRET = 'claude_code'
_SCOPE = ScopeRecipe(
  name='claude',
  harness='claude',
  auth_secret=_AUTH_SECRET,
  llm_key=False,
)


def llm_spec(spec: 'SessionSpec | SessionRun') -> LLMSpec:
  resolved = spec.llm_spec
  if not isinstance(resolved, LLMSpec):
    raise TypeError(f'claude harness resolved an incompatible recipe: {type(resolved).__name__}')
  return resolved


class ClaudeHarness:
  name = 'claude'

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
