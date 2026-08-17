from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol

from bro.launch.scope import ScopeRecipe
from bro.llm.llm import LLMSpec
from bro.workspace.model import Workspace

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import ScopedLaunch, SessionSpec


class Harness(Protocol):
  """the runtime operations supplied by one driving agent loop."""

  name: str

  def add_flags(self, parser: 'Parser') -> None: ...

  def scope_recipe(self, spec: 'SessionSpec') -> ScopeRecipe: ...

  def resolve_llm(self, value: str | None, bro_name: str) -> LLMSpec: ...

  def preflight_auth(self, spec: 'SessionSpec') -> bool: ...

  def inner_command(self, spec: 'SessionSpec') -> list[str]: ...

  def command_options(self, spec: 'SessionSpec') -> tuple[list[str], list[str]]: ...

  def session_exists(self, workspace: Workspace) -> bool: ...

  def trail_pointer(self, workspace_name: str) -> Path: ...

  def read_subject(self, workspace: Workspace) -> str | None: ...

  def drop_workspace(self, workspace: Workspace) -> None: ...

  def launch(
    self,
    spec: 'SessionSpec',
    workspace: Workspace,
    base_ref: Optional[str],
    launch_scope: 'ScopedLaunch',
    *,
    container: bool,
  ) -> int: ...

  def run_in_place(self, spec: 'SessionSpec') -> int: ...


def get_harness(name: str) -> Harness:
  if name == 'claude':
    from ride.claude.harness import CLAUDE

    return CLAUDE
  if name == 'bro':
    from ride.bro import BRO

    return BRO
  raise ValueError(f'unknown harness: {name}')
