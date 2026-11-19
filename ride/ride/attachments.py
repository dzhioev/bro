"""Typed host-provided data placed inside a workspace tree."""

import contextlib
import functools
import importlib.metadata
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENTRY_POINT_GROUP = 'bro.attachment_types'
_TYPE_NAME = re.compile(r'[a-z][a-z0-9-]*')


@dataclass(frozen=True)
class Attachment:
  type: str
  name: str
  fields: dict[str, Any]

  def __post_init__(self) -> None:
    if not isinstance(self.type, str) or _TYPE_NAME.fullmatch(self.type) is None:
      raise ValueError(f'invalid attachment type: {self.type!r}')
    if (
      not isinstance(self.name, str)
      or self.name in ('', '.', '..')
      or Path(self.name).name != self.name
      or '\x00' in self.name
    ):
      raise ValueError(f'invalid attachment name: {self.name!r}')
    if not isinstance(self.fields, dict) or {'type', 'name'} & self.fields.keys():
      raise ValueError('invalid attachment fields')
    get_attachment_type(self.type).validate(self)

  def dump(self) -> dict[str, Any]:
    return {'type': self.type, 'name': self.name, **self.fields}

  @classmethod
  def load(cls, data: dict[str, Any]) -> 'Attachment':
    if not isinstance(data, dict) or not {'type', 'name'} <= data.keys():
      raise ValueError('attachment must be an object with type and name')
    return cls(
      data['type'],
      data['name'],
      {key: value for key, value in data.items() if key not in {'type', 'name'}},
    )


@dataclass(frozen=True)
class AttachmentRequest:
  type: str
  fields: dict[str, Any]

  def dump(self) -> dict[str, Any]:
    return {'type': self.type, 'fields': self.fields}

  @classmethod
  def load(cls, data: dict[str, Any]) -> 'AttachmentRequest':
    if (
      not isinstance(data, dict)
      or data.keys() != {'type', 'fields'}
      or not isinstance(data['fields'], dict)
    ):
      raise ValueError('invalid attachment request')
    get_attachment_type(data['type'])
    return cls(**data)


@dataclass(frozen=True)
class ResolvedAttachment:
  attachment: Attachment
  base: str | None = None


class AttachmentType(ABC):
  name: str

  @abstractmethod
  def validate(self, attachment: Attachment) -> None: ...

  @abstractmethod
  def resolve(
    self, request: AttachmentRequest, workspace: str
  ) -> contextlib.AbstractContextManager[ResolvedAttachment]: ...

  def launch_arguments(self, attachment: Attachment) -> tuple[str, ...]:
    return ()

  @abstractmethod
  def identity(self, attachment: Attachment) -> object: ...

  @abstractmethod
  def prepare(
    self, attachment: Attachment, directory: Path, workspace: Path, base: str | None
  ) -> None:
    """Prepare or reuse an attachment, once per session start."""
    ...

  @abstractmethod
  def environment(self, attachment: Attachment, directory: Path) -> Mapping[str, str | None]:
    """Session variables; None reserves a name and leaves it unset."""
    ...

  @abstractmethod
  def start_session(self, attachment: Attachment, directory: Path, bro: str) -> None: ...

  @abstractmethod
  def inherit(
    self, attachment: Attachment, directory: Path, into: str | None
  ) -> AttachmentRequest: ...

  @abstractmethod
  def cleanliness(
    self, attachment: Attachment, directory: Path, session_end: tuple[bool, list[str]]
  ) -> tuple[bool, list[str]]: ...

  @abstractmethod
  def remove(self, attachment: Attachment, directory: Path, *, force: bool) -> None: ...

  @abstractmethod
  def summary(self, attachment: Attachment) -> str: ...

  @abstractmethod
  def clean_shared(self, attachments: Sequence[Attachment], *, dry_run: bool) -> None: ...


@functools.cache
def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def installed_attachment_types() -> tuple[str, ...]:
  names = [entry.name for entry in _entry_points()]
  if len(names) != len(set(names)):
    raise ValueError('duplicate attachment type entry points')
  if any(_TYPE_NAME.fullmatch(name) is None for name in names):
    raise ValueError('invalid attachment type entry point name')
  return tuple(sorted(names))


def get_attachment_type(name: str) -> AttachmentType:
  if name not in installed_attachment_types():
    raise ValueError(f'attachment type {name!r} is not installed')
  loaded = next(entry for entry in _entry_points() if entry.name == name).load()
  if not isinstance(loaded, AttachmentType):
    raise TypeError(f'attachment type {name!r} must load an AttachmentType instance')
  if loaded.name != name:
    raise ValueError(f'attachment type {name!r} loads a type named {loaded.name!r}')
  return loaded


@contextlib.contextmanager
def resolve_attachment(
  request: AttachmentRequest | None, workspace: str
) -> Iterator[ResolvedAttachment | None]:
  if request is None:
    yield None
  else:
    with get_attachment_type(request.type).resolve(request, workspace) as resolved:
      yield resolved


def attachment_environment(attachments: Sequence[Attachment], tree: Path) -> dict[str, str | None]:
  return {
    key: value
    for attachment in attachments
    for key, value in get_attachment_type(attachment.type)
    .environment(attachment, tree / attachment.name)
    .items()
  }


def inherited_attachment(
  attachments: Sequence[Attachment], tree: Path, into: str | None
) -> AttachmentRequest | None:
  if not attachments:
    if into is not None:
      raise ValueError('into requires an attachment')
    return None
  if len(attachments) != 1:
    raise ValueError('a launch supports at most one attachment')
  attachment = attachments[0]
  return get_attachment_type(attachment.type).inherit(attachment, tree / attachment.name, into)


def apply_attachment_environment(
  environment: dict[str, str], attachments: Sequence[Attachment], tree: Path
) -> None:
  for key, value in attachment_environment(attachments, tree).items():
    if value is None:
      environment.pop(key, None)
    else:
      environment[key] = value
