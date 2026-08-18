import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.launch.identity import bro_git_identity_env
from bro.launch.llm_flags import resolve_native
from bro.launch.scope import BRO_RUN_RECIPE, ScopeRecipe
from bro.launch.trails import local_trails_mounts
from bro.llm.llm import NativeLLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import trail_pointer
from bro.workspace.model import Workspace
from bro.workspace.store import ScopedSecrets
from ride.harness import ContainerExtras

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import SessionSpec


@dataclass(frozen=True)
class BroOptions:
  rich: bool
  text: bool
  no_trails: bool
  subject: Optional[str]

  def dump(self) -> dict:
    return dataclasses.asdict(self)

  @classmethod
  def load(cls, data: dict) -> 'BroOptions':
    if data.keys() != {'rich', 'text', 'no_trails', 'subject'}:
      raise ValueError(f'unexpected bro option fields: {sorted(data.keys())}')
    rich = data['rich']
    text = data['text']
    no_trails = data['no_trails']
    subject = data['subject']
    if (
      not isinstance(rich, bool)
      or not isinstance(text, bool)
      or not isinstance(no_trails, bool)
      or (subject is not None and not isinstance(subject, str))
    ):
      raise TypeError('invalid bro harness options')
    return cls(rich=rich, text=text, no_trails=no_trails, subject=subject)


def add_flags(parser: 'Parser') -> None:
  parser.add_argument(
    '--rich',
    action='store_true',
    help='bro harness, solo: render activity as colored Rich panels',
  )
  parser.add_argument(
    '--text',
    action='store_true',
    help='bro harness, along: force the text conversation instead of the Textual UI',
  )
  parser.add_argument(
    '--no-trails',
    dest='no_trails',
    action='store_true',
    help='bro harness: disable native trail recording',
  )


def options(spec: 'SessionSpec') -> BroOptions:
  return BroOptions.load(spec.harness_options)


def _inner_arguments(spec: 'SessionSpec', resume_trail: Optional[str]) -> list[str]:
  bro = options(spec)
  arguments: list[str] = []
  if spec.prompt is not None:
    arguments.append(spec.prompt)
  if spec.solo and bro.rich:
    arguments.append('--rich')
  if not spec.solo and bro.text:
    arguments.append('--text')
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

  def add_flags(self, parser: 'Parser') -> None:
    add_flags(parser)

  def scope_recipe(self, spec: 'SessionSpec') -> ScopeRecipe:
    if not options(spec).no_trails:
      return BRO_RUN_RECIPE
    return dataclasses.replace(BRO_RUN_RECIPE, optional_baseline=frozenset())

  def resolve_llm(self, value: str | None, bro_name: str) -> NativeLLMSpec:
    from bro.registry import get_class

    selection = LLMSelection() if value is None else parse(value)
    return resolve_native(get_class(bro_name).llm_spec, selection)

  def preflight_auth(self, spec: 'SessionSpec') -> bool:
    del spec
    return True

  def command_options(self, spec: 'SessionSpec') -> tuple[list[str], list[str]]:
    bro = options(spec)
    flags = [
      *(('--rich',) if bro.rich else ()),
      *(('--text',) if bro.text else ()),
      *(('--no-trails',) if bro.no_trails else ()),
    ]
    return flags, []

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
    return None if spec is None else options(spec).subject

  def session_trail_pointer(self, workspace: Workspace) -> Path:
    return trail_pointer.broker_pointer(workspace.path)

  def inner_command(self, spec: 'SessionSpec', workspace: Workspace) -> list[str]:
    resume_trail: Optional[str] = None
    if spec.resume:
      resume_trail = trail_pointer.read(self.session_trail_pointer(workspace))
      if resume_trail is None:
        raise ValueError(f'no bro harness trail recorded for workspace {workspace.name!r}')
    verb = 'run' if spec.solo else 'chat'
    return ['bro', verb, spec.bro, *_inner_arguments(spec, resume_trail), '--in-place']

  def container_extras(
    self, spec: 'SessionSpec', workspace: Workspace, scoped: ScopedSecrets
  ) -> ContainerExtras:
    del workspace
    env = dict(bro_git_identity_env(spec.bro))
    if options(spec).no_trails:
      # a run that records nothing binds no trails root
      env['TRAILS_DISABLED'] = '1'
      return ContainerExtras(env=env, mounts=())
    return ContainerExtras(env=env, mounts=local_trails_mounts(scoped))

  def prepare_host_env(
    self, spec: 'SessionSpec', workspace: Workspace, worktree: Path, env: dict[str, str]
  ) -> None:
    del workspace, worktree
    env.update(bro_git_identity_env(spec.bro))
    env['RIDE_BRO'] = spec.bro
    if options(spec).no_trails:
      env['TRAILS_DISABLED'] = '1'


BRO = BroHarness()
