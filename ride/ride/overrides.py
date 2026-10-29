"""Operator authority transitions and their durable operation records."""

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import Any, cast

from bro.authority.records import dump_authority
from bro.authority.rules import Mission, Override
from bro.authority.syntax import parse_layer
from bro.base import credentials
from bro.base.lulid import lulid
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from bro.workspace.paths import workspace_dir
from ride.authority_launch import credential_values, mission_view, replace_operator_layer
from ride.authority_reading import Reading
from ride.peer_facts import PeerFacts, WorkerFacts


def changes(value: Any) -> list[tuple[str, str]]:
  if not isinstance(value, list) or not value:
    raise ValueError('override check: at least one --grant, --revoke or --cred is required')
  result = []
  registry = credentials.default_registry()
  for item in value:
    if (
      not isinstance(item, (list, tuple))
      or len(item) != 2
      or item[0] not in ('grant', 'revoke', 'cred')
      or not isinstance(item[1], str)
    ):
      raise ValueError(f'override check: invalid change {item!r}')
    verb, name = item
    if name.startswith('@'):
      if verb == 'cred' or re.fullmatch(r'@[a-z][a-z0-9-]*', name) is None:
        raise ValueError(f'override check: expected @<bro> or credential kind: {item!r}')
    else:
      try:
        kind, instance = credentials.parse_name(name)
      except ValueError as error:
        raise ValueError(f'override check: {error}') from error
      if kind not in registry or (verb == 'cred') != (instance is not None):
        raise ValueError(
          f'override check: expected a registered kind, or --cred kind+instance: {item!r}'
        )
    result.append((verb, name))
  return result


def candidate(mission: Mission, requested: list[tuple[str, str]], reading: Reading) -> Mission:
  gift, cap = mission.operator_gift, mission.operator_cap
  values = dict(mission.request.values)
  latest = {name: verb for verb, name in requested if name.startswith('@')}
  for name, verb in latest.items():
    layer = reading.syntax.layer(
      parse_layer(grant=[name]) if verb == 'grant' else parse_layer(revoke=[name])
    )
    gift, cap = replace_operator_layer(gift, cap, layer.gift, layer.cap)
  for verb, name in requested:
    if name.startswith('@'):
      continue
    kind, instance = credentials.parse_name(name)
    update = (
      {'cred.' + kind: cast(str, instance)}
      if verb == 'cred'
      else {'add' if verb == 'grant' else 'drop': frozenset({kind})}
    )
    values = credential_values(values, update)
  variable = {
    name: value
    for name, value in values.items()
    if (name.startswith('cred.') or name in ('add', 'drop'))
    and (
      mission.request.values.get(name) != value
      or any(
        verb == 'cred' and name == 'cred.' + changed.split('+', 1)[0] for verb, changed in requested
      )
    )
  }
  cleared = frozenset(name for name in mission.request.values if name not in values)
  result = reading.rules.override(
    mission, Override(values=variable, clear=cleared, gift=gift, cap=cap), reading.operator()
  )
  for name in latest:
    if name[1:] not in reading.installation.bros:
      raise ValueError(f'existence check: bro.target={name[1:]}: bro is not installed')
  return result


def atomic_record(path: Path, data: Mapping[str, Any]) -> None:
  descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.override-')
  temporary = Path(name)
  with contextlib.ExitStack() as stack:
    stack.callback(temporary.unlink, missing_ok=True)
    with os.fdopen(descriptor, 'w') as stream:
      json.dump(data, stream, indent=2)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def pending(facts: WorkerFacts, operation: dict[str, Any]) -> Generator[None]:
  facts.pending_override = operation
  try:
    yield
  finally:
    facts.pending_override = None


def apply(
  peers: PeerFacts, journal: Journal, reading: Reading, args: Mapping[str, Any]
) -> dict[str, Any]:
  if set(args) != {'id', 'changes'} or not isinstance(args['id'], str):
    raise ValueError('override check: id and changes required')
  requested = changes(args['changes'])
  mission_id = args['id']
  record = journal.records.get(mission_id)
  if record is None or mission_id not in {row['id'] for row in peers.missions()}:
    raise ValueError(f'override check: {mission_id!r} is not a mission of this root')
  facts = peers.for_mission(mission_id)
  if record.state != 'started' or facts.type != 'bro':
    raise ValueError(f'override check: {mission_id!r} is not a running bro session')
  if facts.pending_override is not None:
    raise ValueError(f'override check: operation {facts.pending_override["id"]} is pending')
  mission = facts.record
  if not isinstance(mission, Mission):
    raise ValueError('override check: mission has no authority record')
  if any(not name.startswith('@') for _, name in requested):
    raise ValueError('override check: credential changes require a session applier')
  proposed = candidate(mission, requested, reading)
  latest = {name: verb for verb, name in requested}
  operation = {
    'id': lulid(),
    'kind': 'authority',
    'changes': [[verb, name] for name, verb in latest.items()],
    'operator_gift': dump_authority(proposed.operator_gift, proposed.descriptor),
    'operator_cap': dump_authority(proposed.operator_cap, proposed.descriptor),
  }
  value = {
    'operation': operation['id'],
    'status': 'applied',
    'changes': operation['changes'],
    'may_summon': mission_view(proposed)['may_summon'],
  }
  with pending(facts, operation):
    if facts.workspace is None:
      raise ValueError('override check: mission has no workspace')
    path = workspace_dir(facts.workspace)
    if facts.member is not None:
      atomic_record(party_member_dir(path, facts.member) / 'override.json', operation)
    else:
      resume = path / 'resume.json'
      if resume.exists():
        data = json.loads(resume.read_text())
        data.update(
          operator_gift=operation['operator_gift'],
          operator_cap=operation['operator_cap'],
          override_operation=operation,
        )
        atomic_record(resume, data)
    facts.record = proposed
    journal.override(record, value)
  return value
