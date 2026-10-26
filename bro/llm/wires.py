"""Installed wire decode sides, resolved independently of their clients."""

import importlib.metadata
import re
from typing import Protocol, cast

from bro.llm.records import Chaining, DecodedCall

ENTRY_POINT_GROUP = 'bro.llm.wires'


class DecodeSide(Protocol):
  NAME: str
  CHAINING: Chaining

  def decode(self, response: dict) -> DecodedCall: ...

  def counts(self, usage: dict) -> dict[str, int]: ...


class WireUnavailable(ValueError):
  """A record's wire is not installed, or no wire claims untagged records."""


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def _entries() -> dict[str, importlib.metadata.EntryPoint]:
  entries: dict[str, importlib.metadata.EntryPoint] = {}
  for entry in _entry_points():
    if re.fullmatch(r'[a-z][a-z0-9-]*', entry.name) is None:
      raise ValueError(f'invalid wire name {entry.name!r}')
    if entry.name in entries:
      raise ValueError(f'duplicate wire {entry.name!r}')
    entries[entry.name] = entry
  return entries


def known_names() -> tuple[str, ...]:
  return tuple(sorted(_entries()))


class WireRegistry:
  def __init__(self) -> None:
    self._modules: dict[str, DecodeSide] = {}
    self._legacy: str | None = None

  def get(self, name: str) -> DecodeSide:
    if name not in self._modules:
      entries = _entries()
      if name not in entries:
        raise WireUnavailable(
          f'unknown wire {name!r}; installed wires: {", ".join(sorted(entries)) or "(none)"}'
        )
      try:
        module = entries[name].load()
      except Exception as error:
        raise RuntimeError(f'failed to load wire {name!r}: {error}') from error
      if getattr(module, 'NAME', None) != name:
        raise ValueError(f'wire entry {name!r} must load a decode side with the same NAME')
      if getattr(module, 'CHAINING', None) not in {'server', 'client'}:
        raise ValueError(f'wire {name!r} must declare server or client CHAINING')
      if not callable(getattr(module, 'decode', None)) or not callable(
        getattr(module, 'counts', None)
      ):
        raise TypeError(f'wire {name!r} must export decode and counts')
      self._modules[name] = cast(DecodeSide, module)
    return self._modules[name]

  def legacy_name(self) -> str:
    if self._legacy is not None:
      return self._legacy
    claimants = [name for name in known_names() if getattr(self.get(name), 'LEGACY', False)]
    if len(claimants) > 1:
      raise ValueError(f'multiple wires claim untagged records: {", ".join(claimants)}')
    if not claimants:
      raise WireUnavailable('no installed wire claims untagged records')
    self._legacy = claimants[0]
    return self._legacy

  def decode(self, name: str | None, response: dict) -> DecodedCall:
    return self.get(self.legacy_name() if name is None else name).decode(response)


registry = WireRegistry()


def decode(name: str | None, response: dict) -> DecodedCall:
  return registry.decode(name, response)


def counts(name: str, raw: dict) -> dict[str, int]:
  return registry.get(name).counts(raw)
