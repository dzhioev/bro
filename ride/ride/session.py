import dataclasses
import json
import os
import sys
from dataclasses import dataclass, replace
from typing import Optional

from bro.base import log
from bro.launch.scope import LaunchScopeError, preflight_scoped_launch, scoped_secrets
from bro.llm.llm import LLMSpec
from bro.workspace.git import resolve_ref
from bro.workspace.metadata import WorkspaceKind
from bro.workspace.model import KindMismatch, SessionBusy, Workspace
from bro.workspace.paths import project_root, require_runtime_root
from bro.workspace.store import ScopedSecrets
from ride.harness import Harness, get_harness


@dataclass(frozen=True)
class SessionSpec:
  """the harness-neutral recipe recorded for one managed session."""

  name: str
  harness: str
  workspace_pinned: bool
  host: bool
  drop: bool
  hold: str
  grant: list[str]
  revoke: list[str]
  llm: Optional[str]
  resolved_llm: dict
  solo: bool
  resume: bool
  into: Optional[str]
  bro: str
  prompt: Optional[str]
  harness_options: dict

  @property
  def session_bro(self) -> str:
    return self.bro

  @property
  def along_default_hold(self) -> str:
    return 'guided' if self.host else 'attended'

  @property
  def default_hold(self) -> str:
    if self.solo:
      return 'unattended'
    return self.along_default_hold

  @property
  def llm_spec(self) -> LLMSpec:
    return LLMSpec.from_dict(self.resolved_llm)

  @property
  def kind(self) -> WorkspaceKind:
    return WorkspaceKind.WORKTREE if self.host else WorkspaceKind.CONTAINER

  def to_command_argv(self) -> list[str]:
    if self.resume:
      return ['ride', 'resume', self.name]
    flags = {'--host': self.host}
    if not self.solo:
      flags['--drop'] = self.drop
    elif not self.workspace_pinned:
      flags['--keep'] = not self.drop
    verb = 'solo' if self.solo else 'along'
    parts = ['ride', verb, *(flag for flag, enabled in flags.items() if enabled)]
    if self.hold != self.default_hold:
      parts.extend(['--hold', self.hold])
    if self.llm is not None:
      parts.extend(['--llm', self.llm])
    parts.extend(['--harness', self.harness])
    if self.workspace_pinned:
      parts.extend(['--workspace', self.name])
    for value in self.grant:
      parts.extend(['--grant', value])
    for value in self.revoke:
      parts.extend(['--revoke', value])
    if self.into is not None:
      parts.extend(['--into', self.into])
    harness_flags, forwarded = get_harness(self.harness).command_options(self)
    parts.extend(harness_flags)
    parts.append(self.bro)
    if self.prompt is not None:
      parts.append(self.prompt)
    if len(forwarded) > 0:
      parts.extend(['--', *forwarded])
    return parts

  def inner_command(self) -> list[str]:
    return get_harness(self.harness).inner_command(self)

  def resume_variant(self) -> 'SessionSpec':
    return replace(
      self,
      drop=False,
      hold=self.along_default_hold if self.solo else self.hold,
      solo=False,
      resume=True,
      into=None,
      prompt=None,
      harness_options=_without_create_options(self.harness, self.harness_options),
    )

  def with_scope_overrides(self, *, grant: list[str], revoke: list[str]) -> 'SessionSpec':
    for values, own, flag in ((grant, self.grant, 'grant'), (revoke, self.revoke, 'revoke')):
      restated = sorted(set(values) & set(own))
      if len(restated) > 0:
        raise ValueError(f'already in the recorded --{flag}: {", ".join(restated)}')
    kept_grant = [name for name in self.grant if name not in revoke]
    kept_revoke = [name for name in self.revoke if name not in grant]
    return replace(
      self,
      grant=[*kept_grant, *(name for name in grant if name not in self.revoke)],
      revoke=[*kept_revoke, *(name for name in revoke if name not in self.grant)],
    )

  def dump(self) -> dict:
    return dataclasses.asdict(self)

  @classmethod
  def load(cls, data: dict) -> 'SessionSpec':
    fields = {field.name for field in dataclasses.fields(cls)}
    if data.keys() != fields:
      raise ValueError(f'unexpected fields: {sorted(data.keys() ^ fields)}')
    return cls(**data)


def _without_create_options(harness: str, values: dict) -> dict:
  if harness == 'claude':
    from ride.claude.harness import ClaudeOptions

    options = ClaudeOptions.load(values)
    return ClaudeOptions(raw=options.raw, arguments=[]).dump()
  if harness == 'bro':
    from ride.bro import BroOptions

    return BroOptions.load(values).dump()
  raise ValueError(f'unknown harness: {harness}')


@dataclass(frozen=True)
class ScopedLaunch:
  scoped: ScopedSecrets
  may_summon: set[str]
  store: dict[str, bytes]


def record_resume_spec(workspace: Workspace, spec: SessionSpec) -> None:
  workspace.resume_file.write_text(json.dumps(spec.resume_variant().dump(), indent=2))


def load_resume_spec(workspace: Workspace) -> Optional[SessionSpec]:
  try:
    data = json.loads(workspace.resume_file.read_text())
  except FileNotFoundError:
    return None
  try:
    spec = SessionSpec.load(data)
    get_harness(spec.harness)
    return spec
  except (TypeError, ValueError) as error:
    log.warning('ignoring unreadable resume spec for %s: %s', workspace.name, error)
    return None


def harness_for_workspace(workspace: Workspace) -> Harness:
  spec = load_resume_spec(workspace)
  return get_harness('claude' if spec is None else spec.harness)


def _replace_resume_hint(spec: SessionSpec, workspace: Workspace) -> None:
  if not sys.stdout.isatty() or not get_harness(spec.harness).session_exists(workspace):
    return
  sys.stdout.write('\033[2A\033[J')
  print('Resume this session with:')
  print(f'  ride resume {workspace.name}')


def _finish_session(spec: SessionSpec, workspace: Workspace, code: int) -> int:
  if spec.drop:
    if code == 0:
      try:
        get_harness(spec.harness).drop_workspace(workspace)
        log.info('removed workspace %s', workspace.name)
      except (RuntimeError, OSError) as error:
        log.warning('could not fully remove workspace %s: %s', workspace.name, error)
    else:
      log.info('session exited with code %d; keeping workspace %s', code, workspace.name)
  elif code == 0:
    _replace_resume_hint(spec, workspace)
  return code


def start_session(spec: SessionSpec) -> int:
  harness = get_harness(spec.harness)
  container = not spec.host
  if os.environ.get('RIDE_IN_CONTAINER') is not None:
    log.error(
      'ride cannot start inside a managed container yet; use `summon` for an isolated sibling '
      'or `bro run|chat` for this container and credential scope'
    )
    return 1
  os.environ['RIDE_COMMAND'] = ' '.join(spec.to_command_argv())
  os.environ['RIDE_WORKSPACE'] = spec.name
  os.environ.setdefault('BRO_SHELL_COMMAND', os.environ['RIDE_COMMAND'])

  project = project_root()
  try:
    require_runtime_root(project)
  except RuntimeError as error:
    log.error('%s', error)
    return 1
  base_ref: Optional[str] = None
  if spec.into is not None:
    base_ref = resolve_ref(project, spec.into)
    if base_ref is None:
      log.error('cannot resolve --into ref: %s', spec.into)
      return 1

  if not harness.preflight_auth(spec):
    return 1
  try:
    scoped, may_summon, store = preflight_scoped_launch(
      scoped_secrets(spec.session_bro, harness.scope_recipe(spec), llm_spec=spec.llm_spec),
      spec.session_bro,
      grant=spec.grant,
      revoke=spec.revoke,
    )
  except LaunchScopeError as error:
    log.error('%s', error)
    return 1

  try:
    workspace = Workspace.ensure(spec.name, project, spec.kind)
  except KindMismatch as error:
    log.error('%s', error)
    return 1
  launch = ScopedLaunch(scoped=scoped, may_summon=may_summon, store=store)
  try:
    with workspace.hold_session_lock():
      record_resume_spec(workspace, spec)
      code = harness.launch(spec, workspace, base_ref, launch, container=container)
      return _finish_session(spec, workspace, code)
  except SessionBusy as error:
    log.error('%s', error)
    return 1


def resume_session(name: str, *, grant: list[str], revoke: list[str]) -> int:
  project = project_root()
  try:
    require_runtime_root(project)
  except RuntimeError as error:
    log.error('%s', error)
    return 1
  try:
    workspace = Workspace.open(name, project)
  except ValueError as error:
    log.error('%s', error)
    return 1
  spec = load_resume_spec(workspace)
  if spec is None:
    log.error('no session recorded for %s; start a new one instead', name)
    return 1
  try:
    spec = spec.with_scope_overrides(grant=grant, revoke=revoke)
  except ValueError as error:
    log.error('%s', error)
    return 1
  return start_session(spec)
