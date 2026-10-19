"""Operator authority transitions and their durable operation records."""

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import Any

from bro.authority.records import dump_authority
from bro.authority.rules import Mission, Override
from bro.authority.syntax import parse_layer
from bro.base.lulid import lulid
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from bro.workspace.paths import workspace_dir
from ride.authority_launch import mission_view, replace_operator_layer
from ride.authority_reading import Reading
from ride.peer_facts import PeerFacts, WorkerFacts


def changes(value: Any) -> list[tuple[str, str]]:
  if not isinstance(value, list) or not value:
    raise ValueError('override check: at least one --grant or --revoke @bro is required')
  result = []
  for item in value:
    if (
      not isinstance(item, (list, tuple))
      or len(item) != 2
      or item[0] not in ('grant', 'revoke')
      or not isinstance(item[1], str)
      or re.fullmatch(r'@[a-z][a-z0-9-]*', item[1]) is None
    ):
      raise ValueError(f'override check: only --grant/--revoke @<bro> names can change: {item!r}')
    result.append((item[0], item[1]))
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
  if set(args) != {'name', 'changes'} or not isinstance(args['name'], str):
    raise ValueError('override check: name and changes required')
  requested = changes(args['changes'])
  matches = [row for row in peers.missions() if row['name'] == args['name']]
  if len(matches) != 1:
    raise ValueError(f'override check: name {args["name"]!r} does not identify one mission')
  mission_id = matches[0]['id']
  record = journal.records.get(mission_id)
  facts = peers.for_mission(mission_id)
  if record is None or record.state != 'started' or facts.type != 'bro':
    raise ValueError(f'override check: {args["name"]!r} is not a running bro session')
  if facts.pending_override is not None:
    raise ValueError(f'override check: operation {facts.pending_override["id"]} is pending')
  mission = facts.record
  if not isinstance(mission, Mission):
    raise ValueError('override check: mission has no authority record')
  gift, cap = mission.operator_gift, mission.operator_cap
  latest = {name: verb for verb, name in requested}
  for name, verb in latest.items():
    layer = reading.syntax.layer(
      parse_layer(grant=[name]) if verb == 'grant' else parse_layer(revoke=[name])
    )
    gift, cap = replace_operator_layer(gift, cap, layer.gift, layer.cap)
  candidate = reading.rules.override(mission, Override(gift=gift, cap=cap), reading.operator())
  for name in latest:
    if name[1:] not in reading.installation.bros:
      raise ValueError(f'existence check: bro.target={name[1:]}: bro is not installed')
  operation = {
    'id': lulid(),
    'kind': 'authority',
    'changes': [[verb, name] for name, verb in latest.items()],
    'operator_gift': dump_authority(gift, candidate.descriptor),
    'operator_cap': dump_authority(cap, candidate.descriptor),
  }
  value = {
    'operation': operation['id'],
    'status': 'applied',
    'changes': operation['changes'],
    'may_summon': mission_view(candidate)['may_summon'],
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
    facts.record = candidate
    journal.override(record, value)
  return value
