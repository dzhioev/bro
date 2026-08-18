from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from bro.base import credentials, log
from bro.launch.scope import ScopeRecipe
from bro.llm.llms.claude_code import LLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.workspace.model import Workspace
from ride.claude.claude_auth import _load_anthropic_key
from ride.claude.claude_config import (
  _latest_jsonl,
  read_subject,
  workspace_projects_dir,
)

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import ScopedLaunch, SessionSpec


_FULL_SCOPE = ScopeRecipe(
  name='claude-full',
  harness='claude',
  auth_secret='claude_code',
  llm_key=False,
  docker_sock=True,
  unknown_bro_fallback=True,
)
_RAW_SCOPE = ScopeRecipe(
  name='claude-raw',
  harness='bro',
  auth_secret='anthropic',
  llm_key=False,
  docker_sock=None,
  unknown_bro_fallback=True,
)


@dataclass(frozen=True)
class ClaudeOptions:
  raw: bool
  arguments: list[str]

  def dump(self) -> dict:
    return {'raw': self.raw, 'arguments': self.arguments}

  @classmethod
  def load(cls, data: dict) -> 'ClaudeOptions':
    if data.keys() != {'raw', 'arguments'}:
      raise ValueError(f'unexpected claude option fields: {sorted(data.keys())}')
    raw = data['raw']
    arguments = data['arguments']
    if (
      not isinstance(raw, bool)
      or not isinstance(arguments, list)
      or not all(isinstance(value, str) for value in arguments)
    ):
      raise TypeError('invalid claude harness options')
    return cls(raw=raw, arguments=arguments)


def add_flags(parser: 'Parser') -> None:
  parser.add_argument(
    '--raw',
    action='store_true',
    help="run bare Claude under the bro's prompt and MCP toolset; container only, requires `anthropic`",
  )


def options(spec: 'SessionSpec') -> ClaudeOptions:
  return ClaudeOptions.load(spec.harness_options)


def scope_recipe(raw: bool) -> ScopeRecipe:
  return _RAW_SCOPE if raw else _FULL_SCOPE


def llm_spec(spec: 'SessionSpec') -> LLMSpec:
  resolved = spec.llm_spec
  if not isinstance(resolved, LLMSpec):
    raise TypeError(f'claude harness resolved an incompatible recipe: {type(resolved).__name__}')
  return resolved


class ClaudeHarness:
  name = 'claude'

  def add_flags(self, parser: 'Parser') -> None:
    add_flags(parser)

  def scope_recipe(self, spec: 'SessionSpec') -> ScopeRecipe:
    return scope_recipe(options(spec).raw)

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

  def preflight_auth(self, spec: 'SessionSpec') -> bool:
    if options(spec).raw:
      if _load_anthropic_key() is not None:
        return True
      log.error(
        '--raw requires the `anthropic` secret to provide an api_key '
        '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
      )
      return False
    if credentials.try_get('claude_code') is not None:
      return True
    log.error(
      'claude_code secret not resolvable — a Claude session authenticates with the '
      'setup-token; mint one with `claude setup-token` and store it in '
      '~/.bro/claude_code_oauth_token'
    )
    return False

  def inner_command(self, spec: 'SessionSpec') -> list[str]:
    claude = options(spec)
    verb = 'solo' if spec.solo else 'along'
    flags = {'--resume': spec.resume, '--raw': claude.raw}
    parts = [
      'ride',
      verb,
      '--in-place',
      '--workspace',
      spec.name,
      '--harness',
      'claude',
      *(flag for flag, enabled in flags.items() if enabled),
    ]
    parts.extend(['--hold', spec.hold])
    if spec.llm is not None:
      parts.extend(['--llm', spec.llm])
    parts.append(spec.bro)
    if spec.prompt is not None:
      parts.append(spec.prompt)
    if len(claude.arguments) > 0:
      parts.extend(['--', *claude.arguments])
    return parts

  def command_options(self, spec: 'SessionSpec') -> tuple[list[str], list[str]]:
    claude = options(spec)
    flags = ['--raw'] if claude.raw else []
    return flags, claude.arguments

  def session_exists(self, workspace: Workspace) -> bool:
    return _latest_jsonl(workspace_projects_dir(workspace)) is not None

  def read_subject(self, workspace: Workspace) -> str | None:
    return read_subject(workspace)

  def launch(
    self,
    spec: 'SessionSpec',
    workspace: Workspace,
    base_ref: Optional[str],
    launch_scope: 'ScopedLaunch',
    *,
    container: bool,
  ) -> int:
    from ride.claude.session import launch_session

    return launch_session(spec, workspace, base_ref, launch_scope, container=container)

  def run_in_place(self, spec: 'SessionSpec') -> int:
    from ride.claude.runner import run_in_place

    return run_in_place(spec)


CLAUDE = ClaudeHarness()
