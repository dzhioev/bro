"""Harness identity and the installed harness registry."""

import functools
import importlib.metadata
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from bro.base.condition import Variables

if TYPE_CHECKING:
  from bro.bro import BaseBro, LiveRun
  from bro.llm.mcp import Tool
  from bro.mcp import MCPServerSpec, Reach

SessionEndReason = Literal['ok', 'raised']

ENTRY_POINT_GROUP = 'bro.harnesses'
_NAME = re.compile(r'[a-z][a-z0-9-]*')


@dataclass(frozen=True)
class Service:
  """How one harness serves a reach: the servers it mounts for the reach's groups,
  and the groups it leaves unserved, by name."""

  server_specs: tuple['MCPServerSpec', ...] = ()
  unserved: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnedEnvironment:
  """Environment variables carrying session state:
  every name under its namespaces, and the variables named outside them."""

  namespaces: tuple[str, ...] = ()
  variables: tuple[str, ...] = ()

  def owns(self, name: str) -> bool:
    return name.startswith(self.namespaces) or name in self.variables


class Harness:
  """The framework-visible interface of one driving harness."""

  name: str

  def __init__(self, name: str | None = None):
    if name is not None:
      self.name = harness_name(name)
    else:
      harness_name(self.name)

  def facts(self) -> Variables:
    """Typed facts and passages this harness contributes to prompt rendering."""
    return {}

  def prompt_instructions(self) -> str:
    """Additional instructions this harness contributes to composed prompts."""
    return ''

  def can_end_session(self) -> bool:
    """Whether this harness can end its current session."""
    return False

  def owned_environment(self) -> OwnedEnvironment:
    """The environment variables this harness's sessions carry its state in."""
    return OwnedEnvironment()

  def serve(self, reach: 'Reach') -> Service:
    """How this harness serves `reach`; a harness serving no group leaves each unserved."""
    return Service(unserved=tuple(group.key.name for group in reach.groups))

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


@functools.cache
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
