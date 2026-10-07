"""Trail-format interface and installed format registry."""

import importlib.metadata
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional, cast

from bro.trails.lineage import LineageDecision
from bro.trails.model import BlazeRequest

ENTRY_POINT_GROUP = 'bro.trail_formats'
_FORMAT_NAME = re.compile(r'[a-z][a-z0-9-]*')

# the native header fields a store folds from a trail's rows
SERVER_DERIVED_NATIVE_FIELDS = frozenset({'usage', 'step_counts_by_kind', 'lineage_head'})

# the wire reason a blaze answers when the trail its verdict attaches to advanced
# between the verification and the write, leaving the awarded spans off by that
# much; the caller's next attempt resolves against the extent it reached
ATTACH_CONTENDED = 'the trail advanced while attaching'

# the native fields a mint settles for good: `segment` is the key the trail's
# index answers a lineage lookup on, so an attaching lifetime restamps its own
# facts over the header but never the identity that lookup found it by
_MINTED_NATIVE_FIELDS = frozenset({'segment'})


@dataclass(frozen=True)
class ParsedRecord:
  kind: Optional[str]
  body: Any
  timestamp: Optional[str]
  attributes: dict[str, Any]
  native: dict[str, Any]


@dataclass(frozen=True)
class Classification:
  turn_delta: int = 0
  usage_model: Optional[str] = None
  usage: Optional[dict] = None
  billing_key: Optional[str] = None
  native_updates: Optional[dict[str, Any]] = None
  subject: Optional[str] = None


@dataclass(frozen=True)
class OpenedBody:
  records: list[Any]


@dataclass(frozen=True)
class HeaderRestamp:
  values: dict[str, Any]
  removed: frozenset[str]


@dataclass(frozen=True)
class TrailFormat:
  name: str
  parse: Callable[[Any], ParsedRecord]
  classify: Callable[[ParsedRecord], Classification]
  project: Callable[[dict], list[dict]]
  open: Callable[[dict], OpenedBody]
  validate_create: Callable[[dict], None]
  emitted_message_types: frozenset[str]
  requires_bro: bool
  owner: Callable[[dict], Optional[str]]
  native_header_fields: Callable[[dict], list[tuple[str, Any]]]
  resolve_lineage: Optional[Callable[[dict, Any], LineageDecision]] = None

  def __post_init__(self) -> None:
    format_name(self.name)


def format_name(value: str) -> str:
  """Validate and return a trail-format name."""
  if not isinstance(value, str) or _FORMAT_NAME.fullmatch(value) is None:
    raise ValueError(f'invalid trail-format name {value!r}; expected [a-z][a-z0-9-]*')
  return value


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def installed_format_names() -> tuple[str, ...]:
  """Installed trail-format names, read without importing their implementations."""
  names: set[str] = set()
  for entry in _entry_points():
    format_name(entry.name)
    if entry.name in names:
      raise ValueError(f'duplicate trail format {entry.name!r}')
    names.add(entry.name)
  return tuple(sorted(names))


def _format_not_installed(name: str, installed_names: tuple[str, ...]) -> ValueError:
  installed = ', '.join(installed_names) or '(none)'
  return ValueError(f'trail format {name!r} is not installed; installed trail formats: {installed}')


def get_format(name: str) -> TrailFormat:
  """Load one installed trail format lazily."""
  format_name(name)
  matches = [entry for entry in _entry_points() if entry.name == name]
  if len(matches) == 0:
    raise _format_not_installed(name, installed_format_names())
  if len(matches) > 1:
    raise ValueError(f'duplicate trail format {name!r}')
  loaded = matches[0].load()
  if not isinstance(loaded, TrailFormat):
    raise TypeError(f'trail-format entry point {name!r} must load a TrailFormat instance')
  trail_format = cast(TrailFormat, loaded)
  if trail_format.name != name:
    raise ValueError(
      f'trail-format entry point {name!r} loads a format named {trail_format.name!r}'
    )
  return trail_format


class FormatRegistry:
  """A process-local cache over the installed trail-format entry points."""

  def __init__(self):
    self._formats: dict[str, TrailFormat] = {}

  def get(self, name: str) -> TrailFormat:
    if name not in self._formats:
      self._formats[name] = get_format(name)
    return self._formats[name]


def resolve_lineage(
  trail_format: TrailFormat, request: BlazeRequest, index: Any
) -> LineageDecision:
  """The trail format's verdict for a blaze carrying lineage evidence."""
  assert request.lineage is not None
  if trail_format.resolve_lineage is None:
    raise ValueError(f'the {request.harness} trail format does not resolve lineage')
  return trail_format.resolve_lineage(request.lineage, index)


def blaze_result(
  trail_id: str, started_at: str, extent: int, decision: Optional[LineageDecision]
) -> dict[str, Any]:
  """The blaze response: the recording trail's identity and the ordinal its
  writer appends from, plus the resolver's verdict when the request carried
  lineage evidence. An attached trail answers as itself, so its extent is
  whatever it already recorded."""
  result: dict[str, Any] = {'id': trail_id, 'started_at': started_at, 'extent': extent}
  if decision is not None:
    result['adopted'] = True
    result['chunks'] = decision.chunks
    if decision.attach_to is None:
      result['forked_from'] = decision.forked_from
    else:
      result['attached'] = True
    if decision.reason is not None:
      result['reason'] = decision.reason
  return result


def attached_header(header: dict, request: BlazeRequest) -> HeaderRestamp:
  """The header values a trail takes on when a lifetime attaches to it: the facts
  the blaze would have minted a trail with, latest-wins over the ones the previous
  lifetime left, and the end mark cleared so the trail is open again. `summoned_by`
  is left alone, since the attribution belongs to the run that opened the trail,
  and the server-derived native fold survives the merge because `validate_create`
  refuses a writer those fields."""
  restamped = {
    key: value for key, value in request.native.items() if key not in _MINTED_NATIVE_FIELDS
  }
  values = {
    'end': None,
    'version': request.version,
    'hold': request.hold,
    'location': request.location,
    'native': {**header.get('native', {}), **restamped},
  }
  if request.git is not None:
    values['git'] = request.git
  removed = frozenset({'git'}) if request.git is None else frozenset()
  return HeaderRestamp(values, removed)


def add_numeric_maps(left: dict, right: dict) -> dict:
  """Add numeric leaves while preserving the provider's raw usage vocabulary."""
  result = dict(left)
  for key, value in right.items():
    current = result.get(key)
    if isinstance(value, dict):
      result[key] = add_numeric_maps(current if isinstance(current, dict) else {}, value)
    elif isinstance(value, int) and not isinstance(value, bool):
      result[key] = int(current) + value if isinstance(current, int) else value
  return result


def projected_source(record: dict, index: int = 0) -> dict:
  return {'step_id': record['step_id'], 'index': index}


def projected_event(record: dict, event_type: str, index: int = 0, **fields: Any) -> dict:
  return {
    'type': event_type,
    'ts': record.get('ts'),
    'source': projected_source(record, index),
    **fields,
  }


def parse_json_object(raw: str) -> Optional[dict]:
  try:
    value = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return value if isinstance(value, dict) else None


def validate_server_derived(native: dict) -> None:
  sent = SERVER_DERIVED_NATIVE_FIELDS & set(native)
  if len(sent) > 0:
    raise ValueError(f'native {", ".join(sorted(sent))} are server-derived')
