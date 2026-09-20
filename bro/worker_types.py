"""The core contract and registry for broker-launched worker types."""

from __future__ import annotations

import importlib.metadata
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
  from bro.broker.brotocol import Talk
  from bro.broker.job import CommandJob
  from bro.broker.journal import Event, Record

WORKER_TYPE_GROUP = 'bro.worker_types'
_TYPE_NAME = re.compile(r'[a-z][a-z0-9-]*')


class LaunchDenied(Exception):
  """A worker type refused a launch request."""


class UnattributablePeer(Exception):
  """The host has no complete description for a requesting peer."""


class ArtifactDenied(Exception):
  """An artifact operation the host refuses."""


@dataclass(frozen=True)
class PeerDescription:
  mission: str
  workspace: str
  tree: Path
  type: str
  bro: str | None
  permits: frozenset[str]
  member: str | None
  expected: bool
  artifact_view: bool
  published_ports: tuple[tuple[int, int], ...]
  depth: int
  extension: Any = None


@dataclass(frozen=True)
class LaunchRequest:
  id: str
  type: str
  args: dict[str, Any]
  owner: PeerDescription
  requested_talk: Talk
  timeout: float | None
  share: tuple[str, ...]
  manual: bool


@dataclass(frozen=True)
class Spawn:
  launch: Any
  spawner: Any
  extension: Any = None
  permits: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Job:
  command: CommandJob
  extension: Any = None
  permits: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Expect:
  pending: dict[str, Any]
  extension: Any = None
  permits: frozenset[str] = frozenset()


Run = Spawn | Job | Expect
JournalSubscriber = Callable[['Event', 'Record'], None]


class PeerDirectory(Protocol):
  def describe(self, mission_id: str) -> PeerDescription: ...


class ArtifactResolver(Protocol):
  def resolve(self, ref: str, peer: PeerDescription) -> Path: ...


class Host(Protocol):
  @property
  def peers(self) -> PeerDirectory: ...

  @property
  def artifacts(self) -> ArtifactResolver: ...

  @property
  def credential_kinds(self) -> frozenset[str]: ...


class WorkerType(ABC):
  name: str
  permits: frozenset[str] = frozenset()
  default_timeout: float | None = None
  widens_talk: bool = False
  manual: bool = False

  def __init__(self, host: Host):
    self.host = host

  @abstractmethod
  def talk(self, request: LaunchRequest) -> Talk:
    """Return the effective talk rights for a validated common request."""

  @abstractmethod
  def launch(self, request: LaunchRequest) -> Run:
    """Validate and authorize the type-owned arguments, then choose a host run."""

  def audit_fields(self, extension: Any) -> Mapping[str, Any]:
    return {}

  def subscribers(self) -> Sequence[JournalSubscriber]:
    return ()


def type_name(value: str) -> str:
  if not isinstance(value, str) or _TYPE_NAME.fullmatch(value) is None:
    raise ValueError(f'invalid worker type {value!r}; expected [a-z][a-z0-9-]*')
  return value


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=WORKER_TYPE_GROUP))


def installed_type_names() -> tuple[str, ...]:
  return tuple(sorted({entry.name for entry in _entry_points()}))


def _load(entry: importlib.metadata.EntryPoint) -> type[WorkerType]:
  type_name(entry.name)
  loaded = entry.load()
  if not isinstance(loaded, type) or not issubclass(loaded, WorkerType):
    raise TypeError(f'worker type entry point {entry.name!r} must load a WorkerType subclass')
  worker_type = cast(type[WorkerType], loaded)
  if worker_type.name != entry.name:
    raise ValueError(
      f'worker type entry point {entry.name!r} loads class named {worker_type.name!r}'
    )
  declared = tuple(worker_type.permits)
  if len(declared) != len(set(declared)):
    raise ValueError(f'worker type {entry.name!r} declares a duplicate permit')
  for permit in declared:
    _permit_leaf(permit)
  return worker_type


def installed_type(name: str) -> type[WorkerType]:
  type_name(name)
  matches = [entry for entry in _entry_points() if entry.name == name]
  if len(matches) == 0:
    available = ', '.join(installed_type_names()) or '(none)'
    raise KeyError(f'unknown worker type {name!r}; installed types: {available}')
  if len(matches) > 1:
    raise ValueError(f'duplicate worker type {name!r}')
  return _load(matches[0])


def installed_types() -> dict[str, type[WorkerType]]:
  types: dict[str, type[WorkerType]] = {}
  for entry in sorted(_entry_points(), key=lambda candidate: candidate.name):
    if entry.name in types:
      raise ValueError(f'duplicate worker type {entry.name!r}')
    types[entry.name] = _load(entry)
  return types


def _permit_leaf(value: str) -> str:
  segments = value.split('.') if isinstance(value, str) else []
  if len(segments) == 0 or any(_TYPE_NAME.fullmatch(segment) is None for segment in segments):
    raise ValueError(
      f'invalid worker permit leaf {value!r}; expected dot-separated [a-z][a-z0-9-]* segments'
    )
  return value


def tree_path(tree: Path, relative: str) -> Path:
  """Resolve a workspace-relative path and reject absolute or escaping paths."""
  if Path(relative).is_absolute():
    raise ValueError(f'{relative!r} must be a path relative to the workspace root')
  resolved = (tree / relative).resolve()
  if not resolved.is_relative_to(tree.resolve()):
    raise ValueError(f'{relative!r} escapes the workspace')
  return resolved
