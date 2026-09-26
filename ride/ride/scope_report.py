"""The launch and credential scope a prospective ride session would hold."""

from pathlib import Path
from typing import Optional

from bro.base import credentials, host_config
from bro.base.scope import launch_names
from bro.workspace.project import project_config
from ride.harness import get_harness
from ride.repository import Repository, as_repository
from ride.scope import (
  LaunchScopeError,
  bind_launch_credentials,
  configured_scope_layers,
  effective_launch,
  launch_llm_spec,
  scoped_secrets,
)


def report_scope(repo: Optional[Repository | Path], bro: Optional[str], harness: str) -> int:
  try:
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
    driver = get_harness(harness)
    recipe = driver.scope_recipe()
    attachment = None if repo is None else repo.identity
    binding = bind_launch_credentials(attachment, bro_name)
    llm_spec = launch_llm_spec(driver, attachment, bro_name, None)
    scoped = scoped_secrets(bro_name, recipe, attachment=attachment, llm_spec=llm_spec)
    launch = effective_launch(
      bro_name,
      configured_scope_layers(attachment, bro_name, attachment_repository=repo),
      grant=(),
      revoke=(),
    )
    registry = credentials.default_registry()
    selection = {kind: instance for kind, instance in scoped.selection.items() if kind in registry}
    store = credentials.Store(registry, credentials.STORE_DIR, selection)
  except (LaunchScopeError, ValueError) as error:
    print(f'cannot compute the scope: {error}')
    return 1
  print(f'repository: {repo.identity if repo is not None else "(detached)"}')
  print(f'bro:        {bro_name} ({recipe.name})')
  print(f'launch:     {", ".join(launch_names(launch)) or "(none)"}')
  _print_tiers(
    [
      ('required', sorted(scoped.required)),
      ('optional', sorted(scoped.optional - scoped.required)),
    ],
    binding,
    store,
  )
  return 0


def _print_tiers(
  tiers: list[tuple[str, list[str]]],
  binding: host_config.CredentialSelection,
  store: credentials.Store,
) -> None:
  """print each non-empty tier under one column layout measured over all of them."""
  names = [name for _, tier in tiers for name in tier]
  if len(names) == 0:
    return
  optional = set(dict(tiers).get('optional', ()))
  present = store.instance_names()
  name_width = max(len(name) for name in names)
  reads_width = max(len(_reads(name, binding, store)) for name in names)
  for label, tier in tiers:
    if len(tier) == 0:
      continue
    print(f'{label}:')
    for name in tier:
      storage = store.selected_name(name)
      if storage in present:
        state = 'PRESENT'
      elif name in optional and name not in store.selection:
        state = 'SKIPPED'
      else:
        state = 'MISSING'
      reads = _reads(name, binding, store)
      print(f'  {name:<{name_width}}  {reads:<{reads_width}}  {state}')


def _reads(
  kind: str,
  binding: host_config.CredentialSelection,
  store: credentials.Store,
) -> str:
  if kind in binding.instances:
    selected = credentials.storage_name(kind, binding.instances[kind])
    return f'{selected} ({binding.layers[kind]})'
  if kind in store.defaults:
    selected = credentials.storage_name(kind, store.defaults[kind])
    return f'{selected} (store defaults)'
  return f'{kind} (unpicked)'
