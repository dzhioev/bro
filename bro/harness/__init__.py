"""Harness identity and the installed harness registry."""

import importlib.metadata
import re
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
  from bro.bro import BaseBro, LiveRun
  from bro.llm.mcp import Tool

SessionEndReason = Literal['ok', 'raised']

ENTRY_POINT_GROUP = 'bro.harnesses'
_NAME = re.compile(r'[a-z][a-z0-9-]*')


class Harness:
  """The framework-visible interface of one driving harness."""

  name: str

  def __init__(self, name: str | None = None):
    if name is not None:
      self.name = harness_name(name)
    else:
      harness_name(self.name)

  def can_end_session(self) -> bool:
    """Whether this harness can end its current session."""
    return False

  def own_tools(self, bro: 'BaseBro', live_run: 'LiveRun | None') -> tuple['Tool', ...]:
    """Service tools this harness serves itself for the selected declaration."""
    del bro, live_run
    return ()

  async def end_session(self, result: str, end_reason: SessionEndReason) -> str:
    """End the current session with its terminal result."""
    del result, end_reason
    raise RuntimeError(f'harness {self.name!r} cannot end this session')


def harness_name(value: str) -> str:
  """Validate and return a harness name."""
  if not isinstance(value, str) or _NAME.fullmatch(value) is None:
    raise ValueError(f'invalid harness name {value!r}; expected [a-z][a-z0-9-]*')
  return value


def name_of(harness: Harness | str) -> str:
  """Return the validated name carried by a harness object or declaration value."""
  return harness.name if isinstance(harness, Harness) else harness_name(harness)


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def installed_harness_names() -> tuple[str, ...]:
  """Installed harness names, read without importing their implementations."""
  names: set[str] = set()
  for entry in _entry_points():
    harness_name(entry.name)
    if entry.name in names:
      raise ValueError(f'duplicate harness {entry.name!r}')
    names.add(entry.name)
  return tuple(sorted(names))


def get_harness(name: str) -> Harness:
  """Load one installed harness lazily."""
  harness_name(name)
  matches = [entry for entry in _entry_points() if entry.name == name]
  if len(matches) == 0:
    installed = ', '.join(installed_harness_names()) or '(none)'
    raise ValueError(f'harness {name!r} is not installed; installed harnesses: {installed}')
  if len(matches) > 1:
    raise ValueError(f'duplicate harness {name!r}')
  loaded = matches[0].load()
  if not isinstance(loaded, Harness):
    raise TypeError(f'harness entry point {name!r} must load a Harness instance')
  harness = cast(Harness, loaded)
  if harness.name != name:
    raise ValueError(f'harness entry point {name!r} loads a harness named {harness.name!r}')
  return harness
