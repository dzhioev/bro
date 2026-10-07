import json
import subprocess
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Optional

from bro.base import log, spawn
from bro.harness import Harness, SessionEndReason
from bro.launch.llm_flags import resolve_native
from bro.llm.llm import NativeLLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import trail_pointer
from ride.harness import ContainerExtras
from ride.scope import ScopeRecipe
from ride.workspace.model import Workspace
from ride.workspace.store import ScopedSecrets

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


def _session_arguments(spec: 'SessionSpec | SessionRun', resume_trail: Optional[str]) -> list[str]:
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


class BroHarness(Harness):
  name = 'bro'

  def can_end_session(self) -> bool:
    return True

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
      spec.bro,
      *_session_arguments(spec, resume_trail),
      *spec.arguments,
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
