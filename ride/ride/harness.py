from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from bro.launch.scope import ScopeRecipe
from bro.llm.llm import LLMSpec
from bro.workspace.model import Workspace
from bro.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import SessionSpec


@dataclass(frozen=True)
class ContainerExtras:
  """what one harness adds to the neutral container launch."""

  env: dict[str, str]
  mounts: tuple[str, ...]


class Harness(Protocol):
  """the runtime operations supplied by one driving agent loop."""

  name: str

  def add_flags(self, parser: 'Parser') -> None: ...

  def scope_recipe(self, spec: 'SessionSpec') -> ScopeRecipe: ...

  def resolve_llm(self, value: str | None, bro_name: str) -> LLMSpec: ...

  def preflight_auth(self, spec: 'SessionSpec') -> bool: ...

  def command_options(self, spec: 'SessionSpec') -> tuple[list[str], list[str]]: ...

  def session_exists(self, workspace: Workspace) -> bool: ...

  def missing_session_error(self, workspace: Workspace) -> str: ...

  def read_subject(self, workspace: Workspace) -> str | None: ...

  def session_trail_pointer(self, workspace: Workspace) -> Path: ...

  def inner_command(self, spec: 'SessionSpec', workspace: Workspace) -> list[str]: ...

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras: ...

  def prepare_host_env(
    self, spec: 'SessionSpec', workspace: Workspace, worktree: Path, env: dict[str, str]
  ) -> None: ...


def get_harness(name: str) -> Harness:
  if name == 'claude':
    from ride.claude.harness import CLAUDE

    return CLAUDE
  if name == 'bro':
    from ride.bro import BRO

    return BRO
  raise ValueError(f'unknown harness: {name}')
