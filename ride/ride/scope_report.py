"""the credential scope a prospective ride session would hydrate."""

from typing import Optional

from bro.base import credentials
from bro.launch.scope import (
  BRO_RUN_RECIPE,
  LaunchScopeError,
  bind_project_credentials,
  scoped_secrets,
)
from bro.workspace.paths import project_root
from bro.workspace.project import project_config
from ride.claude.harness import scope_recipe as claude_scope_recipe
from ride.harness import get_harness


def report_scope(
  bro: Optional[str],
  raw: bool,
  harness: Optional[str] = None,
  label: Optional[str] = None,
) -> int:
  project = project_root()
  config = project_config()
  bro_name = bro if bro is not None else config.default_bro
  harness_name = harness or config.harness
  try:
    get_harness(harness_name)
    if harness_name == 'claude':
      recipe = claude_scope_recipe(raw)
    else:
      if raw:
        raise ValueError('--raw requires the claude harness')
      recipe = BRO_RUN_RECIPE
    selection = bind_project_credentials()
    scoped = scoped_secrets(bro_name, recipe)
  except (LaunchScopeError, ValueError) as error:
    print(f'cannot compute the scope: {error}')
    return 1
  print(f'project: {project}')
  print(f'bro:     {bro_name} ({label or recipe.name})')
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
  if kind not in selection:
    return ''
  instance = selection[kind]
  return f'{kind}+{instance} (project)' if instance is not None else f'{kind} (project)'
