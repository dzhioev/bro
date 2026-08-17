import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.launch.llm_flags import resolve_native
from bro.launch.scope import BRO_RUN_RECIPE, ScopeRecipe
from bro.llm.llm import NativeLLMSpec
from bro.llm.providers import LLMSelection, parse
from bro.monitor import trail_pointer
from bro.workspace.model import Workspace

if TYPE_CHECKING:
  from bro.base.args import Parser
  from ride.session import ScopedLaunch, SessionSpec


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


def session_trail_pointer(workspace_name: str) -> Path:
  from bro.workspace.paths import project_root, workspace_dir

  return workspace_dir(project_root(), workspace_name) / trail_pointer.FILENAME


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

  def host_fallback_error(self, spec: 'SessionSpec') -> Optional[str]:
    del spec
    return None

  def inner_command(self, spec: 'SessionSpec') -> list[str]:
    from ride.bro_session import inner_command

    return inner_command(spec)

  def command_options(self, spec: 'SessionSpec') -> tuple[list[str], list[str]]:
    bro = options(spec)
    flags = [
      *(('--rich',) if bro.rich else ()),
      *(('--text',) if bro.text else ()),
      *(('--no-trails',) if bro.no_trails else ()),
    ]
    return flags, []

  def session_exists(self, workspace: Workspace) -> bool:
    return trail_pointer.read(self.trail_pointer(workspace.name)) is not None

  def trail_pointer(self, workspace_name: str) -> Path:
    return session_trail_pointer(workspace_name)

  def read_subject(self, workspace: Workspace) -> str | None:
    from ride.session import load_resume_spec

    spec = load_resume_spec(workspace)
    return None if spec is None else options(spec).subject

  def drop_workspace(self, workspace: Workspace) -> None:
    workspace.remove()

  def launch(
    self,
    spec: 'SessionSpec',
    workspace: Workspace,
    base_ref: Optional[str],
    launch_scope: 'ScopedLaunch',
    *,
    container: bool,
  ) -> int:
    from ride.bro_session import launch_session

    return launch_session(spec, workspace, base_ref, launch_scope, container=container)

  def run_in_place(self, spec: 'SessionSpec') -> int:
    raise ValueError(f'{spec.interface} has no bro-harness in-place runner')


BRO = BroHarness()
