"""Installed file types and process-local lazy loading."""

import importlib.metadata
import threading
from typing import Optional, cast

from bro.files.types import FileType, type_name

ENTRY_POINT_GROUP = 'bro.file_types'
_NO_TYPES = 'no file types installed; install a distribution providing one, such as bro-filetypes'


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def installed_type_names() -> tuple[str, ...]:
  names: set[str] = set()
  for entry in _entry_points():
    type_name(entry.name)
    if entry.name in names:
      raise ValueError(f'duplicate file type {entry.name!r}')
    names.add(entry.name)
  return tuple(sorted(names))


def get_type(name: str) -> FileType:
  type_name(name)
  entries = _entry_points()
  matches = [entry for entry in entries if entry.name == name]
  if not matches:
    names = installed_type_names()
    installed = ', '.join(names) or '(none)'
    hint = f'; {_NO_TYPES}' if not names else ''
    raise ValueError(f'unknown file type {name!r}; installed file types: {installed}{hint}')
  if len(matches) > 1:
    raise ValueError(f'duplicate file type {name!r}')
  entry = matches[0]
  try:
    loaded = entry.load()
  except Exception as error:
    raise RuntimeError(f'failed to load file type {name!r}: {error}') from error
  if not isinstance(loaded, FileType):
    raise TypeError(f'file-type entry point {name!r} must load a FileType instance')
  file_type = cast(FileType, loaded)
  if file_type.name != name:
    raise ValueError(f'file-type entry point {name!r} loads a type named {file_type.name!r}')
  return file_type


class TypeRegistry:
  def __init__(self) -> None:
    self._types: dict[str, FileType] = {}
    self._lock = threading.RLock()

  def get(self, name: str) -> FileType:
    with self._lock:
      if name not in self._types:
        self._types[name] = get_type(name)
      return self._types[name]

  def identify(self, head: bytes) -> FileType:
    names = installed_type_names()
    if not names:
      raise ValueError(_NO_TYPES)
    matches: list[FileType] = []
    for name in names:
      try:
        file_type = self.get(name)
      except Exception as error:
        raise RuntimeError(f'failed to load file type {name!r}: {error}') from error
      if any(signature.matches(head) for signature in file_type.signatures):
        matches.append(file_type)
    if not matches:
      raise ValueError(
        f'no file type matches; installed file types: {", ".join(names)}; pass type explicitly'
      )
    if len(matches) > 1:
      raise ValueError(
        f'ambiguous file type: {", ".join(match.name for match in matches)}; pass type explicitly'
      )
    return matches[0]

  def resolve(self, head: bytes, type: Optional[str] = None) -> FileType:
    return self.identify(head) if type is None else self.get(type)


registry = TypeRegistry()


def identify(head: bytes) -> FileType:
  return registry.identify(head)
