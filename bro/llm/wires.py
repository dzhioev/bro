"""Installed wire decode sides and clients, resolved independently."""

import importlib.metadata
import re
from typing import Protocol, cast

from bro.llm.llm import LLMSpec
from bro.llm.records import Chaining, DecodedCall
from bro.llm.wire import Wire

ENTRY_POINT_GROUP = 'bro.llm.wires'


class DecodeSide(Protocol):
  NAME: str
  CHAINING: Chaining

  def decode(self, response: dict) -> DecodedCall: ...

  def counts(self, usage: dict) -> dict[str, int]: ...


class WireUnavailable(ValueError):
  """A wire or its client is unavailable."""


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


CLIENT_ENTRY_POINT_GROUP = 'bro.llm.wire_clients'


def _client_entries() -> dict[str, importlib.metadata.EntryPoint]:
  entries: dict[str, importlib.metadata.EntryPoint] = {}
  for entry in importlib.metadata.entry_points(group=CLIENT_ENTRY_POINT_GROUP):
    if re.fullmatch(r'[a-z][a-z0-9-]*', entry.name) is None:
      raise ValueError(f'invalid wire client name {entry.name!r}')
    if entry.name in entries:
      raise ValueError(f'duplicate wire client {entry.name!r}')
    entries[entry.name] = entry
  return entries


def client_names() -> tuple[str, ...]:
  return tuple(sorted(_client_entries()))


def load_client(name: str) -> type[Wire]:
  entries = _client_entries()
  if name not in entries:
    raise WireUnavailable(
      f'wire {name!r} has no client; install a client for that wire or select another recipe'
    )
  entry = entries[name]
  try:
    client = entry.load()
  except ImportError as error:
    assert entry.dist is not None
    distribution = entry.dist.metadata['Name']
    raise WireUnavailable(
      f'wire {name!r} client cannot be imported: {error}; '
      f'install the client extra of {distribution} ({distribution}[client])'
    ) from error
  if not isinstance(client, type) or not issubclass(client, Wire) or client.NAME != name:
    raise TypeError(f'wire client entry {name!r} must load a Wire class with the same NAME')
  if client.CHAINING != registry.get(name).CHAINING:
    raise ValueError(f'wire client {name!r} disagrees with its decode side on CHAINING')
  return client


def open(recipe: LLMSpec, secrets: dict) -> Wire:
  client = load_client(recipe.WIRE)
  conflicts = client.OWNED & recipe.request_options().keys()
  if conflicts:
    raise ValueError(
      f'recipe {recipe.TYPE!r} sets wire-owned fields: {", ".join(sorted(conflicts))}'
    )
  return client(recipe, secrets)
