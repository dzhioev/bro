from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol

from bro.llm.llm import LLMSpec
from ride.scope import ScopeRecipe
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

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

  def add_flags(self, parser: 'Parser') -> tuple[str, ...]: ...

  def parse_options(self, args: dict, *, solo: bool, host: bool) -> dict: ...

  def default_options(self) -> dict: ...

  def scope_recipe(self, options: dict) -> ScopeRecipe: ...

  def resolve_llm(self, value: str | None, bro_name: str) -> LLMSpec: ...

  def preflight_auth(self, spec: 'SessionSpec') -> Optional[str]: ...

  def command_options(self, spec: 'SessionSpec') -> list[str]: ...

  def session_exists(self, workspace: Workspace) -> bool: ...

  def missing_session_error(self, workspace: Workspace) -> str: ...

  def read_subject(self, workspace: Workspace) -> str | None: ...

  def inner_command(self, spec: 'SessionSpec', workspace: Workspace) -> list[str]: ...

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras: ...

  def prepare_host_env(
    self, spec: 'SessionSpec', workspace: Workspace, worktree: Path, env: dict[str, str]
  ) -> None: ...


HARNESS_NAMES = ('claude', 'bro')


def get_harness(name: str) -> Harness:
  if name == 'claude':
    from ride.claude.harness import CLAUDE

    return CLAUDE
  if name == 'bro':
    from ride.bro import BRO

    return BRO
  raise ValueError(f'unknown harness: {name}')
