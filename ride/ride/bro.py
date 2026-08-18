import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.launch.llm_flags import resolve_native
from bro.llm.llm import NativeLLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import trail_pointer
from bro.workspace.model import Workspace
from bro.workspace.store import ScopedSecrets
from ride.harness import ContainerExtras
from ride.identity import bro_git_identity_env
from ride.scope import BRO_RUN_RECIPE, ScopeRecipe

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import SessionSpec


def _inner_arguments(spec: 'SessionSpec', resume_trail: Optional[str]) -> list[str]:
  arguments: list[str] = []
  if spec.prompt is not None:
    arguments.append(spec.prompt)
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


class BroHarness:
  name = 'bro'

  def add_flags(self, parser: 'Parser') -> tuple[str, ...]:
    del parser
    return ()

  def parse_options(self, args: dict, *, solo: bool, host: bool) -> dict:
    del args, solo, host
    return {}

  def default_options(self) -> dict:
    return {}

  def scope_recipe(self, options: dict) -> ScopeRecipe:
    del options
    return BRO_RUN_RECIPE

  def resolve_llm(self, value: str | None, bro_name: str) -> NativeLLMSpec:
    from bro.registry import get_class

    selection = LLMSelection() if value is None else parse(value)
    return resolve_native(get_class(bro_name).llm_spec, selection)

  def preflight_auth(self, spec: 'SessionSpec') -> Optional[str]:
    del spec
    return None

  def command_options(self, spec: 'SessionSpec') -> list[str]:
    del spec
    return []

  def session_exists(self, workspace: Workspace) -> bool:
    return trail_pointer.read(self.session_trail_pointer(workspace)) is not None

  def missing_session_error(self, workspace: Workspace) -> str:
    return (
      f'cannot resume bro harness workspace {workspace.name!r}: no trail pointer was published; '
      'the session may have run without a broker or with --no-trails'
    )

  def read_subject(self, workspace: Workspace) -> str | None:
    from ride.session import load_resume_spec

    spec = load_resume_spec(workspace)
    return None if spec is None else spec.subject

  def session_trail_pointer(self, workspace: Workspace) -> Path:
    return trail_pointer.broker_pointer(workspace.path)

  def inner_command(self, spec: 'SessionSpec', workspace: Workspace) -> list[str]:
    resume_trail: Optional[str] = None
    if spec.resume:
      resume_trail = trail_pointer.read(self.session_trail_pointer(workspace))
      if resume_trail is None:
        raise ValueError(f'no bro harness trail recorded for workspace {workspace.name!r}')
    verb = 'run' if spec.solo else 'chat'
    return [
      'bro',
      verb,
      spec.bro,
      *_inner_arguments(spec, resume_trail),
      *spec.arguments,
      '--in-place',
    ]

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras:
    del workspace, scoped
    return ContainerExtras(env=dict(bro_git_identity_env(spec.bro)), mounts=())

  def prepare_host_env(
    self, spec: 'SessionSpec', workspace: Workspace, worktree: Path, env: dict[str, str]
  ) -> None:
    del workspace, worktree
    env.update(bro_git_identity_env(spec.bro))
    env['RIDE_BRO'] = spec.bro


BRO = BroHarness()
