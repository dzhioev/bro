from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from bro.harness import get_harness as load_harness, installed_harness_names
from bro.llm.llm import LLMSpec
from ride.scope import ScopeRecipe
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


@dataclass(frozen=True)
class ContainerExtras:
  """what one harness adds to the neutral container launch."""

  env: dict[str, str]
  mounts: tuple[str, ...]


@runtime_checkable
class SessionHarness(Protocol):
  """The managed-session operations supplied by one driving harness."""

  name: str

  def scope_recipe(self) -> ScopeRecipe: ...

  def resolve_llm(self, value: str | None, bro_name: str) -> LLMSpec: ...

  def preflight_auth(self, spec: 'SessionSpec', scoped: ScopedSecrets) -> Optional[str]: ...

  def session_exists(self, workspace: Workspace) -> bool: ...

  def missing_session_error(self, workspace: Workspace) -> str: ...

  def read_subject(self, workspace: Workspace) -> str | None: ...

  def prepare_session(self, run: 'SessionRun') -> None: ...

  def check_runtime(self) -> None: ...

  def clean_cache(self, *, dry_run: bool = False) -> None:
    """Remove unused host-wide caches and report what was removed or retained."""
    ...

  def run_session(self, spec: 'SessionRun') -> int: ...

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras: ...

  def prepare_unboxed_env(
    self, spec: 'SessionSpec', records: Path, tree: Path, env: dict[str, str]
  ) -> None: ...

  def prepare_boxed_member_env(
    self, spec: 'SessionSpec', records: Path, member_root: PurePath, env: dict[str, str]
  ) -> None: ...


HARNESS_NAMES = installed_harness_names()


def get_harness(name: str) -> SessionHarness:
  harness = load_harness(name)
  if not isinstance(harness, SessionHarness):
    raise TypeError(f'installed harness {name!r} does not implement ride SessionHarness')
  return harness
