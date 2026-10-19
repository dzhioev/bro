"""Host-side request mapping, admission, and descriptor credential hydration."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from bro.authority.authority import Authority
from bro.authority.patterns import Descriptor
from bro.authority.records import dump_authority, dump_values
from bro.authority.rules import Mission, Request
from bro.authority.syntax import parse_layer
from bro.authority.values import Value
from bro.base import credentials
from bro.worker_types import WorkerType
from ride.authority_reading import Reading
from ride.authority_scope import existence
from ride.workspace.store import ScopedSecrets


def request(
  worker: type[WorkerType],
  fields: Mapping[str, Any],
  reading: Reading,
  giver: Authority,
  *,
  forward: bool = True,
) -> tuple[Request, Mapping[str, Any]]:
  lists = {}
  for name in ('grant', 'revoke', 'limit'):
    value = fields.get(name, [])
    if not isinstance(value, (list, tuple)) or any(type(item) is not str for item in value):
      raise ValueError(f'declaration check: {name}: expected a list of entries')
    lists[name] = value
  parsed = parse_layer(**lists)
  layer = reading.syntax.layer(parsed)
  mapped = worker.option_values(
    {name: value for name, value in fields.items() if name not in lists}, reading
  )
  values = dict(mapped.values)
  if layer.hold or layer.drop:
    if worker.name != 'bro':
      raise ValueError(f'declaration check: {worker.name}: credential kinds require a bro start')
    for name, members in (('add', layer.hold), ('drop', layer.drop)):
      values[name] = cast(frozenset[str], values.get(name, frozenset())) | members
  gift = Authority()
  for entry in parsed.grant:
    if (
      entry.permission.type in reading.installation.credential_kinds
      and entry.permission.option is None
    ):
      continue
    offered = (
      reading.syntax.forward(entry, giver) if forward else Authority((reading.syntax.entry(entry),))
    )
    gift = gift.join(offered)
  return Request(worker.name, values, gift, layer.cap), mapped.content


def credentials_for(mission: Mission, *, recording: bool = True) -> ScopedSecrets:
  reading = cast(Reading, mission.reading)
  values = mission.descriptor.values
  selection = {
    name.removeprefix('cred.'): cast(str, value)
    for name, value in values.items()
    if name.startswith('cred.')
  }
  store = credentials.Store(credentials.default_registry(), credentials.STORE_DIR, selection)
  names = store.instance_names()
  present = frozenset(
    kind
    for kind, instance in selection.items()
    if credentials.storage_name(kind, instance) in names
  )
  explicit = frozenset(
    name.removeprefix('cred.') for name in mission.request.values if name.startswith('cred.')
  )
  manifest = existence(mission.descriptor, reading, present, explicit=explicit, recording=recording)
  return ScopedSecrets(set(manifest.required), set(manifest.optional), selection)


def hydrate(scoped: ScopedSecrets) -> tuple[dict[str, bytes], frozenset[str]]:
  store = credentials.Store(credentials.default_registry(), credentials.STORE_DIR, scoped.selection)
  return credentials.build_scoped_store(store, scoped.required, optional=scoped.optional)


def mission_view(mission: Mission) -> dict[str, Any]:
  from bro.authority.authority import Atom, Entry, Pair, partition
  from bro.authority.syntax import render_entry

  reading = cast(Reading, mission.reading)
  descriptor = Descriptor('bro', {})
  types = tuple(reading.installation.declarations)
  usable, give_only, targets, summon_entries = [], [], [], []
  entries = []
  for region in partition(mission.authority.permissions()):
    pair = mission.authority.evaluate_region(region, descriptor, types)
    if not pair.is_empty:
      entries.append(Entry(region, pair))
    if not pair.use.is_empty:
      rows = summon_entries if (region.type, region.option) == ('bro', 'target') else usable
      rows.append(render_entry(Entry(region, Pair(use=pair.use)), types))
    elif not pair.give.is_empty:
      give_only.append(render_entry(Entry(region, Pair(give=pair.give)), types))
  for name in reading.installation.bros:
    start = Descriptor('bro', {'target': name})
    atom = Atom('bro', 'target', name)
    use = mission.authority.evaluate(atom, start, types).use.meet(
      mission.authority.evaluate(Atom('bro'), start, types).use
    )
    if use.matches(start, prepare=True):
      targets.append(name)
  return {
    'descriptor': {
      'type': mission.descriptor.type,
      'values': dump_values(mission.descriptor.values),
    },
    'authority': dump_authority(Authority(tuple(entries)), descriptor),
    'operator_values': sorted(mission.operator_values),
    'may_summon': sorted(targets),
    'summon_entries': sorted(summon_entries),
    'permits': sorted(usable),
    'may_give': sorted(give_only),
  }


def credential_values(
  original: Mapping[str, Value], changes: Mapping[str, Value]
) -> dict[str, Value]:
  dropped = cast(frozenset[str], changes.get('drop', frozenset()))
  for kind in dropped:
    if 'cred.' + kind in changes:
      raise ValueError(f'validity check: bro.cred.{kind}: cannot be set and in bro.drop')
  values = dict(original)
  for name in ('add', 'drop'):
    other = 'drop' if name == 'add' else 'add'
    changed = cast(frozenset[str], changes.get(name, frozenset()))
    removed = cast(frozenset[str], changes.get(other, frozenset()))
    if name in values or changed or removed:
      values[name] = (cast(frozenset[str], values.get(name, frozenset())) - removed) | changed
  for name, value in changes.items():
    if name.startswith('cred.'):
      values[name] = value
      values['drop'] = cast(frozenset[str], values.get('drop', frozenset())) - {
        name.removeprefix('cred.')
      }
  for kind in cast(frozenset[str], changes.get('drop', frozenset())):
    values.pop('cred.' + kind, None)
  return values


def resume_request(
  requested: Request, spec: Any, reading: Reading
) -> tuple[Request, Authority, Authority]:
  from bro.authority.authority import Cap
  from bro.authority.records import load_authority
  from ride.bro_worker import BroType

  changes, _ = request(
    BroType,
    {'pass': spec.cred, 'grant': spec.grant, 'revoke': spec.revoke, 'limit': spec.limit},
    reading,
    reading.operator(),
    forward=False,
  )
  values = credential_values(requested.values, changes.values)
  gift = load_authority(spec.operator_gift, reading.syntax) if spec.operator_gift else Authority()
  cap = load_authority(spec.operator_cap, reading.syntax) if spec.operator_cap else Cap()
  gift, cap = replace_operator_layer(gift, cap, changes.gift, changes.cap)
  return replace(requested, values=values), gift, cap


def replace_operator_layer(
  gift: Authority, cap: Authority, added: Authority, removed: Authority
) -> tuple[Authority, Authority]:
  replaced = (*added.permissions(), *removed.permissions())

  def keep(authority: Authority) -> Authority:
    entries = []
    for entry in authority.entries:
      regions = (entry.permission,)
      for replacement in replaced:
        regions = tuple(piece for region in regions for piece in region.difference(replacement))
      entries.extend(replace(entry, permission=region) for region in regions)
    return Authority(
      tuple(entries), authority.operation, tuple(keep(item) for item in authority.operands)
    )

  def compact(authority: Authority) -> Authority:
    if authority.operation == 'top':
      return authority
    entries = list(authority.entries)
    operands = []
    for operand in authority.operands:
      operand = compact(operand)
      if not operand.entries and not operand.operands:
        is_top = operand.operation in ('meet', 'top')
        if is_top == (authority.operation == 'meet'):
          continue
        return Authority.top() if is_top else Authority()
      if operand.operation == authority.operation:
        entries.extend(operand.entries)
        operands.extend(operand.operands)
      else:
        operands.append(operand)
    if not entries and len(operands) == 1:
      return operands[0]
    return Authority(tuple(entries), authority.operation, tuple(operands))

  return compact(keep(gift).join(added)), compact(keep(cap).meet(removed))
