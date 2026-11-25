"""What a managed session's lifetime was launched under, the lifetimes its
records hold, and the notice a resumed lifetime opens with."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from bro.base.credential_apply import atomic_file

if TYPE_CHECKING:
  from bro.llm.llm import Recipe

FILENAME = 'launch-facts.json'
FORMAT = 1

_FACT_KEYS = frozenset(
  {
    'runtime',
    'llm',
    'hold',
    'summoned',
    'talk',
    'may_summon',
    'permits',
    'may_give',
    'credentials',
  }
)
_ROWS = ('may_summon', 'permits', 'may_give')

_OPENING = (
  '[notice: this run continues an earlier one, in a new process.',
  'Jobs, watches and summoned children of the earlier run have ended.',
  'The working directory may have changed since.',
)
_CLOSING = 'The mission and the trail are new: call bro::banner for them.]'
_UNCOMPARED = (
  'The runtime, the recipe, your credentials and what you may summon may have changed too: '
  'spell and reference texts you loaded earlier may differ; '
  'load one again before you rely on its wording.'
)
_TEXTS_DIFFER = (
  'spell and reference texts you loaded earlier may differ: '
  'load one again before you rely on its wording'
)


class LaunchFactsError(ValueError):
  """a launch-facts file of this format that is not of its shape."""


def _strings(value: Any, field: str) -> tuple[str, ...]:
  if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
    raise ValueError(f'{field} must be a list of strings')
  return tuple(value)


def _optional_strings(value: Any, field: str) -> Optional[tuple[str, ...]]:
  return None if value is None else _strings(value, field)


@dataclass(frozen=True)
class LaunchFacts:
  """What one lifetime was launched under, as its banner states it.

  `runtime` is the frozen runtime bundle's hash, None for a materialized one;
  `talk` is None when the session has no quest, and the three authority rows
  None without a broker channel. `credentials` names the pick each held kind
  reads, never its material."""

  runtime: Optional[str]
  llm: str
  hold: str
  summoned: bool
  talk: Optional[tuple[str, ...]]
  may_summon: Optional[tuple[str, ...]]
  permits: Optional[tuple[str, ...]]
  may_give: Optional[tuple[str, ...]]
  credentials: tuple[str, ...]

  @classmethod
  def collect(cls, *, runtime: Optional[str], recipe: 'Recipe', hold: str) -> 'LaunchFacts':
    """The facts of the session this process runs, under the given runtime, admitted recipe and hold."""
    from bro import summon
    from bro.authority_view import initial
    from bro.base import credentials
    from bro.llm.providers import format_recipe
    from bro.workspace.banner import may_summon_row

    authority = initial()
    talk = summon.talk()
    return cls(
      runtime=runtime,
      llm=format_recipe(recipe),
      hold=hold,
      summoned=summon.summoned(),
      talk=talk,
      may_summon=None
      if authority is None
      else may_summon_row(tuple(authority['may_summon']), tuple(authority['summon_entries'])),
      permits=None if authority is None else tuple(authority['permits']),
      may_give=None if authority is None else tuple(authority['may_give']),
      credentials=credentials.default_store().held_picks(),
    )

  def to_wire(self) -> dict[str, Any]:
    return {
      'runtime': self.runtime,
      'llm': self.llm,
      'hold': self.hold,
      'summoned': self.summoned,
      'talk': None if self.talk is None else list(self.talk),
      'may_summon': None if self.may_summon is None else list(self.may_summon),
      'permits': None if self.permits is None else list(self.permits),
      'may_give': None if self.may_give is None else list(self.may_give),
      'credentials': list(self.credentials),
    }

  @classmethod
  def from_wire(cls, value: Any) -> 'LaunchFacts':
    if not isinstance(value, dict) or set(value) != _FACT_KEYS:
      raise ValueError(f'facts must carry exactly {", ".join(sorted(_FACT_KEYS))}')
    if value['runtime'] is not None and not isinstance(value['runtime'], str):
      raise ValueError('runtime must be a string or null')
    for field in ('llm', 'hold'):
      if not isinstance(value[field], str):
        raise ValueError(f'{field} must be a string')
    if not isinstance(value['summoned'], bool):
      raise ValueError('summoned must be a boolean')
    return cls(
      runtime=value['runtime'],
      llm=value['llm'],
      hold=value['hold'],
      summoned=value['summoned'],
      talk=_optional_strings(value['talk'], 'talk'),
      may_summon=_optional_strings(value['may_summon'], 'may_summon'),
      permits=_optional_strings(value['permits'], 'permits'),
      may_give=_optional_strings(value['may_give'], 'may_give'),
      credentials=_strings(value['credentials'], 'credentials'),
    )


@dataclass(frozen=True)
class Launch:
  """A lifetime launched under `facts` that began to write `record` at its item `start`."""

  record: str
  start: int
  facts: LaunchFacts

  def to_wire(self) -> dict[str, Any]:
    return {'record': self.record, 'from': self.start, 'facts': self.facts.to_wire()}

  @classmethod
  def from_wire(cls, value: Any) -> 'Launch':
    if not isinstance(value, dict) or set(value) != {'record', 'from', 'facts'}:
      raise ValueError('a launch must carry exactly record, from and facts')
    record, start = value['record'], value['from']
    if not isinstance(record, str) or len(record) == 0:
      raise ValueError('record must be a non-empty string')
    if type(start) is not int or start < 0:
      raise ValueError('from must be a non-negative integer')
    return cls(record, start, LaunchFacts.from_wire(value['facts']))


@dataclass(frozen=True)
class Launches:
  """A session's `launch-facts.json`, its entries in the order they were registered.

  An absent file and a file of another format hold no entries;
  registering replaces either with a file of this format."""

  path: Path
  entries: tuple[Launch, ...]

  @classmethod
  def load(cls, session_directory: Path) -> 'Launches':
    path = session_directory / FILENAME
    try:
      raw = path.read_bytes()
    except FileNotFoundError:
      return cls(path, ())
    try:
      value = json.loads(raw)
      if not isinstance(value, dict) or type(value.get('format')) is not int:
        raise ValueError('expected an object with an integer format')
      if value['format'] != FORMAT:
        return cls(path, ())
      if set(value) != {'format', 'launches'} or not isinstance(value['launches'], list):
        raise ValueError(f'a format-{FORMAT} file carries exactly format and launches')
      entries = tuple(Launch.from_wire(entry) for entry in value['launches'])
    except ValueError as error:
      raise LaunchFactsError(f'invalid launch facts {path}: {error}') from error
    return cls(path, entries)

  def baseline(self, record: str, extent: int) -> Optional[int]:
    """The index of the newest entry that began `record` below its item `extent`:
    the lifetime that left the last of the record's first `extent` items."""
    found: Optional[int] = None
    for index, entry in enumerate(self.entries):
      if (
        entry.record == record
        and entry.start < extent
        and (found is None or entry.start >= self.entries[found].start)
      ):
        found = index
    return found

  def register(self, record: str, start: int, facts: LaunchFacts) -> 'Launches':
    """Append a lifetime's entry and replace the file with the result."""
    registered = Launches(self.path, (*self.entries, Launch(record, start, facts)))
    content = {'format': FORMAT, 'launches': [entry.to_wire() for entry in registered.entries]}
    atomic_file(self.path, json.dumps(content, ensure_ascii=False).encode())
    return registered


def _value(entries: Optional[tuple[str, ...]]) -> str:
  if entries is None:
    return 'unset'
  return ', '.join(entries) or 'none'


def resume_notice(facts: LaunchFacts, baseline: Optional[LaunchFacts]) -> str:
  """The notice a resumed lifetime launched under `facts` opens with.

  `baseline` holds the facts of the lifetime that last added to the history it
  continues, or is None when that lifetime registered none."""
  if baseline is None:
    return '\n'.join((*_OPENING, _UNCOMPARED, _CLOSING))
  changed: list[str] = []
  unchanged: list[str] = []
  same_runtime = facts.runtime is not None and facts.runtime == baseline.runtime
  if same_runtime:
    unchanged.append('the runtime')
  else:
    changed.append('the runtime was updated')
  if facts.llm == baseline.llm:
    unchanged.append('the recipe')
  else:
    changed.append(f'llm: {baseline.llm} → {facts.llm}')
  if facts.hold != baseline.hold:
    changed.append(f'hold: {baseline.hold} → {facts.hold}')
  if facts.summoned != baseline.summoned:
    changed.append(
      f'summoned: {"yes" if baseline.summoned else "no"} → {"yes" if facts.summoned else "no"}'
    )
  if facts.talk != baseline.talk:
    changed.append(f'talk: {_value(baseline.talk)} → {_value(facts.talk)}')
  if facts.credentials == baseline.credentials:
    unchanged.append('your credentials')
  else:
    changed.append(f'credentials: {_value(baseline.credentials)} → {_value(facts.credentials)}')
  differing = [row for row in _ROWS if getattr(facts, row) != getattr(baseline, row)]
  for row in differing:
    changed.append(f'{row}: {_value(getattr(baseline, row))} → {_value(getattr(facts, row))}')
  if len(differing) == 0:
    unchanged.append('what you may summon')
  if (
    same_runtime
    and facts.hold == baseline.hold
    and facts.credentials == baseline.credentials
    and facts.may_summon == baseline.may_summon
  ):
    unchanged.append('spell and reference texts')
  else:
    changed.append(_TEXTS_DIFFER)
  if len(changed) == 0:
    comparison = [f'Unchanged since then: {"; ".join(unchanged)}.']
  else:
    comparison = ['Changed since then:', *(f'- {line}' for line in changed)]
    if len(unchanged) > 0:
      comparison.append(f'Unchanged: {"; ".join(unchanged)}.')
  return '\n'.join((*_OPENING, *comparison, _CLOSING))
