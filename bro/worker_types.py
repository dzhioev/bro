"""The core contract and registry for broker-launched worker types."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast

from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT

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
class LaunchSet:
  values: Callable[[], Collection[str]]


@dataclass(frozen=True)
class LaunchFlag:
  pass


LAUNCH_FLAG = LaunchFlag()
LaunchFieldSchema = LaunchSet | LaunchFlag
LaunchField = frozenset[str] | bool
LaunchPayload = dict[str, LaunchField]
Launch = dict[str, LaunchPayload]


def parse_launch(value: object, *, subject: str = 'launch') -> Launch:
  if not isinstance(value, Mapping):
    raise ValueError(f'{subject} must be an object')
  launch: Launch = {}
  for raw_type, raw_payload in value.items():
    try:
      worker_type = type_name(raw_type)
    except ValueError as error:
      raise ValueError(f'{subject}: {error}') from error
    if not isinstance(raw_payload, Mapping):
      raise ValueError(f'{subject}.{worker_type} must be an object')
    payload: LaunchPayload = {}
    for raw_field, raw_field_value in raw_payload.items():
      if not isinstance(raw_field, str) or _TYPE_NAME.fullmatch(raw_field) is None:
        raise ValueError(f'{subject}.{worker_type} has invalid field {raw_field!r}')
      if isinstance(raw_field_value, bool):
        payload[raw_field] = raw_field_value
        continue
      if not isinstance(raw_field_value, (list, tuple, set, frozenset)) or not all(
        isinstance(member, str) for member in raw_field_value
      ):
        raise ValueError(
          f'{subject}.{worker_type}.{raw_field} must be a boolean or a set of strings'
        )
      members = tuple(raw_field_value)
      if len(members) != len(set(members)):
        raise ValueError(f'{subject}.{worker_type}.{raw_field} contains a duplicate value')
      payload[raw_field] = frozenset(members)
    launch[worker_type] = payload
  return launch


def dump_launch(launch: Mapping[str, Mapping[str, LaunchField]]) -> dict[str, dict[str, object]]:
  parsed = parse_launch(launch)
  return {
    worker_type: {
      field: sorted(value) if isinstance(value, frozenset) else value
      for field, value in sorted(payload.items())
    }
    for worker_type, payload in sorted(parsed.items())
  }


@dataclass(frozen=True)
class PeerDescription:
  mission: str
  workspace: str
  tree: Path
  type: str
  bro: str | None
  launch: Launch
  member: str | None
  expected: bool
  artifact_view: PurePosixPath | None
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


_RESERVED_CONTAINER_ENV = ('BROKER_', 'RIDE_', 'BRO_')
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


@dataclass(frozen=True)
class WorkerContainer:
  files: Mapping[str, bytes]
  command: tuple[str, ...]
  env: Mapping[str, str]
  published_ports: tuple[int, ...]
  artifact_view: PurePosixPath = PurePosixPath(CONTAINER_ARTIFACTS_ROOT)

  def __post_init__(self) -> None:
    artifact_view = self.artifact_view
    if not isinstance(artifact_view, (str, PurePosixPath)):
      raise ValueError('worker container artifact view must be an absolute POSIX path')
    raw_artifact_view = str(artifact_view)
    normalized_artifact_view = PurePosixPath(raw_artifact_view)
    segments = raw_artifact_view.split('/')[1:]
    if (
      not normalized_artifact_view.is_absolute()
      or raw_artifact_view.startswith('//')
      or '\0' in raw_artifact_view
      or (raw_artifact_view != '/' and any(segment in ('', '.', '..') for segment in segments))
      or str(normalized_artifact_view) != raw_artifact_view
    ):
      raise ValueError('worker container artifact view must be an absolute normalized POSIX path')

    files = dict(self.files)
    if not all(
      isinstance(path, str) and isinstance(content, bytes) for path, content in files.items()
    ):
      raise ValueError('worker container files must map POSIX paths to bytes')
    for path in files:
      segments = path.split('/')
      normalized = str(PurePosixPath(path))
      if (
        len(path) == 0
        or '\0' in path
        or path.startswith('/')
        or any(segment in ('', '.', '..') for segment in segments)
        or normalized != path
      ):
        raise ValueError(
          f'worker container file path {path!r} is not a normalized NUL-free relative POSIX path'
        )
    dockerfile = files.get('Dockerfile')
    if dockerfile is None:
      raise ValueError("worker container files need a 'Dockerfile'")
    try:
      dockerfile_text = dockerfile.decode()
    except UnicodeDecodeError as error:
      raise ValueError('worker container Dockerfile must be UTF-8') from error
    instructions = [
      line.strip()
      for line in dockerfile_text.splitlines()
      if line.strip() and not line.lstrip().startswith('#')
    ]
    try:
      from_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.upper().startswith('FROM ')
      )
    except StopIteration as error:
      raise ValueError('worker container Dockerfile must open FROM ${RUNTIME_IMAGE}') from error
    if not any(
      instruction.upper() == 'ARG RUNTIME_IMAGE' for instruction in instructions[:from_index]
    ) or instructions[from_index].split() != ['FROM', '${RUNTIME_IMAGE}']:
      raise ValueError(
        'worker container Dockerfile must open with ARG RUNTIME_IMAGE and FROM ${RUNTIME_IMAGE}'
      )

    if (
      not isinstance(self.command, tuple)
      or len(self.command) == 0
      or not all(isinstance(argument, str) and '\0' not in argument for argument in self.command)
      or not self.command[0]
    ):
      raise ValueError(
        'worker container command must name an executable in a non-empty string tuple'
      )
    env = dict(self.env)
    if not all(
      isinstance(name, str)
      and _ENV_NAME.fullmatch(name) is not None
      and isinstance(value, str)
      and '\0' not in value
      for name, value in env.items()
    ):
      raise ValueError(
        'worker container env must map environment variable names to NUL-free strings'
      )
    reserved = sorted(
      name for name in env if name in ('HOME', 'PATH') or name.startswith(_RESERVED_CONTAINER_ENV)
    )
    if reserved:
      raise ValueError(f'worker container env names host-owned variable(s): {", ".join(reserved)}')
    ports = self.published_ports
    if not isinstance(ports, tuple) or not all(
      isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 for port in ports
    ):
      raise ValueError('worker container published ports must be a tuple of ports in 1..65535')
    if len(ports) != len(set(ports)):
      raise ValueError('worker container published ports must be distinct')
    object.__setattr__(self, 'files', MappingProxyType(files))
    object.__setattr__(self, 'env', MappingProxyType(env))
    object.__setattr__(self, 'artifact_view', normalized_artifact_view)

  def image_hash(self, runtime_image: str) -> str:
    if not isinstance(runtime_image, str) or not runtime_image:
      raise ValueError('worker container runtime image must be a non-empty string')
    digest = hashlib.sha256()
    runtime_bytes = runtime_image.encode()
    digest.update(len(runtime_bytes).to_bytes(8, 'big'))
    digest.update(runtime_bytes)
    for path, content in sorted(self.files.items()):
      for value in (path.encode(), content):
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    return digest.hexdigest()[:12]


@dataclass(frozen=True)
class Spawn:
  launch: Any
  spawner: Any
  extension: Any = None
  launch_scope: Launch = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
  command: CommandJob
  extension: Any = None
  launch_scope: Launch = field(default_factory=dict)


@dataclass(frozen=True)
class Container:
  spec: WorkerContainer
  extension: Any = None
  launch_scope: Launch = field(default_factory=dict)


@dataclass(frozen=True)
class Expect:
  pending: dict[str, Any]
  extension: Any = None
  launch_scope: Launch = field(default_factory=dict)


Run = Spawn | Job | Container | Expect
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


class WorkerType(ABC):
  name: str
  launch_schema: Mapping[str, LaunchFieldSchema] = MappingProxyType({})
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
  schema = worker_type.launch_schema
  if not isinstance(schema, Mapping):
    raise TypeError(f'worker type {entry.name!r} launch schema must be a mapping')
  for field_name, field_schema in schema.items():
    if not isinstance(field_name, str) or _TYPE_NAME.fullmatch(field_name) is None:
      raise ValueError(f'worker type {entry.name!r} declares invalid launch field {field_name!r}')
    if not isinstance(field_schema, (LaunchSet, LaunchFlag)):
      raise TypeError(
        f'worker type {entry.name!r} launch field {field_name!r} has an invalid schema'
      )
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


def tree_path(tree: Path, relative: str) -> Path:
  """Resolve a workspace-relative path and reject absolute or escaping paths."""
  if Path(relative).is_absolute():
    raise ValueError(f'{relative!r} must be a path relative to the workspace root')
  resolved = (tree / relative).resolve()
  if not resolved.is_relative_to(tree.resolve()):
    raise ValueError(f'{relative!r} escapes the workspace')
  return resolved
