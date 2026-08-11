"""`cw scope`: the credential scope a session launched from this project would
hydrate, and which instance each kind reads."""

from typing import Optional

from bro.base import credentials
from bro.launch.scope import LaunchScopeError, Surface, bind_project_credentials, scoped_secrets
from bro.workspace.paths import project_root
from bro.workspace.project import project_config


def report_scope(bro: Optional[str], raw: bool) -> int:
  project = project_root()
  bro_name = bro if bro is not None else project_config().default_bro
  surface = Surface.RAW_SESSION if raw else Surface.CW_SESSION
  try:
    selection = bind_project_credentials()
    scoped = scoped_secrets(bro_name, surface)
  except (LaunchScopeError, ValueError) as e:
    print(f'cannot compute the scope: {e}')
    return 1
  print(f'project: {project}')
  print(f'bro:     {bro_name} ({surface.value})')
  _print_tier('required', sorted(scoped.required), selection)
  _print_tier('optional', sorted(scoped.optional - scoped.required), selection)
  return 0


def _print_tier(label: str, names: list[str], selection: dict[str, Optional[str]]) -> None:
  if len(names) == 0:
    return
  print(f'{label}:')
  for name in names:
    state = 'ok' if credentials.available(name) else 'MISSING'
    print(f'  {name:<14}{_reads(name, selection):<24}{state}')


def _reads(kind: str, selection: dict[str, Optional[str]]) -> str:
  """the project's selection for this kind, blank where it has none and the kind
  resolves through whatever the host registry binds it to."""
  if kind not in selection:
    return ''
  instance = selection[kind]
  return f'{kind}+{instance} (project)' if instance is not None else f'{kind} (project)'
