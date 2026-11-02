"""Render a prospective descriptor, base authority, and credential presence."""

import json
from pathlib import Path
from typing import cast

from bro.authority.records import dump_values
from bro.authority.values import Strings
from bro.base import credentials
from ride.authority_launch import mission_view, request
from ride.authority_reading import Reading
from ride.authority_scope import existence
from ride.bro_worker import BroType
from ride.repository import Repository, as_repository


def report_scope(
  repo: Repository | Path | None,
  bro: str | None,
  harness: str | None,
  llm: str | None = None,
) -> int:
  try:
    repository = None if repo is None else as_repository(repo)
    reading = Reading.take(repository)
    if bro is None:
      bro = reading.default_bro
    harness = reading.resolve_harness(harness, target=bro)
    fields = {'target': bro, 'harness': harness}
    if llm is not None:
      fields['llm'] = llm
    requested, _ = request(BroType, fields, reading, reading.operator(), forward=False)
    descriptor = reading.rules.normalize(requested, reading)
    mission = reading.rules._mission(requested, descriptor, reading)
    selection = {
      name.removeprefix('cred.'): cast(str, value)
      for name, value in descriptor.values.items()
      if name.startswith('cred.')
    }
    store = credentials.Store(credentials.default_registry(), credentials.STORE_DIR, selection)
    present = store.instance_names()
    present_kinds = frozenset(
      kind
      for kind, instance in selection.items()
      if credentials.storage_name(kind, instance) in present
    )
    manifest = existence(
      descriptor, reading, present_kinds, check_presence=False, require_recipe=False
    )
    from bro.llm.providers import format_recipe
    from ride.authority_scope import checked_recipe, resolve_recipe

    resolved = checked_recipe(descriptor, reading, None if llm is None else (llm, '--llm'))
    configured = resolve_recipe(reading, bro, harness) if llm is None else None
    print(
      'descriptor: '
      + json.dumps(dump_values(descriptor.values), ensure_ascii=False, sort_keys=True)
    )
    print(f'llm: {format_recipe(resolved)} ({configured.source if configured else "--llm"})')
    view = mission_view(mission)
    for key in ('summon_entries', 'permits', 'may_give'):
      print(f'{key}: ' + (', '.join(view[key]) or '(none)'))
    print('credentials:')
    for kind, instance in sorted(selection.items()):
      name = credentials.storage_name(kind, instance)
      if kind not in manifest.required | manifest.optional:
        state = 'SKIPPED'
      elif name in present:
        state = 'PRESENT'
      elif kind in manifest.optional and not instance and kind not in reading.picks(bro):
        state = 'SKIPPED'
      else:
        state = 'MISSING'
      print(f'  {kind}+{instance}: {state}')
    instances = set()
    for permission in mission.authority.permissions():
      if (
        permission.option is not None
        and permission.option.startswith('cred.')
        and isinstance(permission.values, Strings)
        and not permission.values.cofinite
      ):
        kind = permission.option.removeprefix('cred.')
        instances.update((kind, instance) for instance in permission.values.members)
    for kind, instance in sorted(instances):
      state = 'PRESENT' if credentials.storage_name(kind, instance) in present else 'MISSING'
      print(f'authority instance {kind}+{instance}: {state}')
    return 0
  except (ValueError, credentials.SecretNotFound) as error:
    print(f'cannot compute the scope: {error}')
    return 1
