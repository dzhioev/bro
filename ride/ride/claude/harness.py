from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.base import credentials
from bro.llm.llms.claude_code import LLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import CLAUDE_CONFIG_DIR_ENV
from ride.claude.claude_auth import apply_claude_auth, load_anthropic_key
from ride.claude.claude_config import (
  container_claude_state,
  latest_jsonl,
  provision_host_claude_dir,
  read_subject,
  workspace_projects_dir,
)
from ride.harness import ContainerExtras
from ride.scope import ScopeRecipe
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import SessionSpec


_FULL_SCOPE = ScopeRecipe(
  name='claude-full',
  harness='claude',
  auth_secret='claude_code',
  llm_key=False,
)
_RAW_SCOPE = ScopeRecipe(
  name='claude-raw',
  harness='bro',
  auth_secret='anthropic',
  llm_key=False,
)


@dataclass(frozen=True)
class ClaudeOptions:
  raw: bool

  def dump(self) -> dict:
    return {'raw': self.raw}

  @classmethod
  def load(cls, data: dict) -> 'ClaudeOptions':
    if data.keys() != {'raw'}:
      raise ValueError(f'unexpected claude option fields: {sorted(data.keys())}')
    if not isinstance(data['raw'], bool):
      raise TypeError('invalid claude harness options')
    return cls(raw=data['raw'])


def options(spec: 'SessionSpec') -> ClaudeOptions:
  return ClaudeOptions.load(spec.harness_options)


def llm_spec(spec: 'SessionSpec') -> LLMSpec:
  resolved = spec.llm_spec
  if not isinstance(resolved, LLMSpec):
    raise TypeError(f'claude harness resolved an incompatible recipe: {type(resolved).__name__}')
  return resolved


class ClaudeHarness:
  name = 'claude'

  def add_flags(self, parser: 'Parser') -> tuple[str, ...]:
    parser.add_argument(
      '--raw',
      action='store_true',
      help="run bare Claude under the bro's prompt and MCP toolset; container only, requires `anthropic`",
    )
    return ('raw',)

  def parse_options(self, args: dict, *, solo: bool, host: bool) -> dict:
    del solo
    if args['raw'] and host:
      raise ValueError('--raw cannot be combined with --host')
    return ClaudeOptions(raw=args['raw']).dump()

  def default_options(self) -> dict:
    return ClaudeOptions(raw=False).dump()

  def scope_recipe(self, options: dict) -> ScopeRecipe:
    return _RAW_SCOPE if ClaudeOptions.load(options).raw else _FULL_SCOPE

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

  def preflight_auth(self, spec: 'SessionSpec') -> Optional[str]:
    if options(spec).raw:
      if load_anthropic_key() is not None:
        return None
      return (
        '--raw requires the `anthropic` secret to provide an api_key '
        '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
      )
    if credentials.try_get('claude_code') is not None:
      return None
    material_path = credentials.default_store().material_path('claude_code')
    return (
      'claude_code secret not resolvable — a Claude session authenticates with the '
      f'setup-token; mint one with `claude setup-token` and store it at {material_path}; '
      f'see {credentials.CREDENTIAL_MIGRATION_GUIDE}'
    )

  def inner_flags(self, spec: 'SessionSpec') -> tuple[str, ...]:
    return ('--raw',) if options(spec).raw else ()

  def run_in_place(self, spec: 'SessionSpec') -> int:
    from ride.claude.runner import run_in_place

    return run_in_place(spec)

  def command_options(self, spec: 'SessionSpec') -> list[str]:
    return ['--raw'] if options(spec).raw else []

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

  def prepare_host_env(
    self, spec: 'SessionSpec', workspace: Workspace, worktree: Path, env: dict[str, str]
  ) -> None:
    del spec
    repository = workspace.repository
    project = worktree if repository is None else repository.git_dir
    claude_dir = provision_host_claude_dir(workspace.path, worktree, project)
    env[CLAUDE_CONFIG_DIR_ENV] = str(claude_dir)
    apply_claude_auth(env)


CLAUDE = ClaudeHarness()
