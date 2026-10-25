"""File access decisions and regular-file opens."""

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import BinaryIO, Protocol


class FileAccessDenied(PermissionError):
  def __init__(self, path: Path, rule: str):
    self.path = path
    self.rule = rule
    super().__init__(f'file access denied: {path} (rule: {rule})')


class FileAccess(Protocol):
  def open_read(self, path: Path) -> BinaryIO: ...

  def open_write(self, path: Path, *, create_parents: bool = False) -> BinaryIO: ...

  def search_directory(self, path: Path) -> Path: ...


def _require_regular_file(path: Path, status: os.stat_result) -> None:
  if not stat.S_ISREG(status.st_mode):
    raise ValueError(
      f'{path} is not a regular file; refusing to open a FIFO, device, socket, or '
      'directory (the read or write could block forever)'
    )


@contextmanager
def _pinned_path(path: Path) -> Iterator[tuple[Path, os.stat_result]]:
  if hasattr(os, 'O_PATH'):
    descriptor = os.open(path, os.O_PATH)
    try:
      status = os.fstat(descriptor)
      _require_regular_file(path, status)
      yield Path(f'/proc/self/fd/{descriptor}'), status
    finally:
      os.close(descriptor)
  else:
    status = path.stat()
    _require_regular_file(path, status)
    yield path, status


def _open_regular(path: Path, *, write: bool, create_parents: bool = False) -> BinaryIO:
  flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK
  with ExitStack() as pin:
    try:
      target, expected = pin.enter_context(_pinned_path(path))
    except FileNotFoundError:
      if not write:
        raise
      if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
      target, expected = path, None
      flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o666)
    with ExitStack() as owner:
      owner.callback(os.close, descriptor)
      status = os.fstat(descriptor)
      _require_regular_file(path, status)
      if expected is not None and (status.st_dev, status.st_ino) != (
        expected.st_dev,
        expected.st_ino,
      ):
        raise ValueError(f'{path} changed while opening; refusing to use a different file')
      if write:
        os.ftruncate(descriptor, 0)
      stream = os.fdopen(descriptor, 'wb' if write else 'rb')
      owner.pop_all()
      return stream


class AllowAll:
  def open_read(self, path: Path) -> BinaryIO:
    return _open_regular(path, write=False)

  def open_write(self, path: Path, *, create_parents: bool = False) -> BinaryIO:
    return _open_regular(path, write=True, create_parents=create_parents)

  def search_directory(self, path: Path) -> Path:
    return path


_ALLOW_ALL = AllowAll()
_access: ContextVar[FileAccess] = ContextVar('file_access', default=_ALLOW_ALL)


def current() -> FileAccess:
  return _access.get()


@contextmanager
def installed(access: FileAccess) -> Iterator[None]:
  token = _access.set(access)
  try:
    yield
  finally:
    _access.reset(token)
