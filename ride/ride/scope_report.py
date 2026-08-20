"""the credential scope a prospective ride session would hydrate."""

from pathlib import Path
from typing import Optional

from bro.base import credentials
from bro.workspace.project import project_config
from ride.harness import get_harness
from ride.repository import Repository, as_repository
from ride.scope import LaunchScopeError, bind_project_credentials, scoped_secrets


def report_scope(
  repo: Optional[Repository | Path], bro: Optional[str], harness: str, options: dict
) -> int:
  repo = None if repo is None else as_repository(repo)
  if repo is None and bro is None:
    raise ValueError('ride scope requires --bro when detached')
  config = (
    None
    if repo is None
    else (repo.project_config() if repo.is_url else project_config(repo.git_dir))
  )
  if bro is None:
    assert config is not None
    bro_name = config.default_bro
  else:
    bro_name = bro
  recipe = get_harness(harness).scope_recipe(options)
  attachment = None if repo is None else repo.identity
  try:
    selection = bind_project_credentials(attachment)
    scoped = scoped_secrets(bro_name, recipe, attachment=attachment)
  except (LaunchScopeError, ValueError) as error:
    print(f'cannot compute the scope: {error}')
    return 1
  print(f'repository: {repo.identity if repo is not None else "(detached)"}')
  print(f'bro:        {bro_name} ({recipe.name})')
  _print_tier('required', sorted(scoped.required), selection, scoped.unbound_kinds)
  _print_tier(
    'optional', sorted(scoped.optional - scoped.required), selection, scoped.unbound_kinds
  )
  return 0


def _print_tier(
  label: str,
  names: list[str],
  selection: Optional[dict[str, Optional[str]]],
  unbound: frozenset[str],
) -> None:
  if len(names) == 0:
    return
  print(f'{label}:')
  for name in names:
    if name in unbound:
      print(f'  {name:<14}{"per project, unbound":<24}REFUSED')
      continue
    state = 'ok' if credentials.available(name) else 'MISSING'
    print(f'  {name:<14}{_reads(name, selection):<24}{state}')


def _reads(kind: str, selection: Optional[dict[str, Optional[str]]]) -> str:
  if selection is None or kind not in selection:
    return ''
  instance = selection[kind]
  return f'{kind}+{instance} (project)' if instance is not None else f'{kind} (project)'
