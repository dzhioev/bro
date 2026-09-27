"""Host-wide lifetime, build, and removal locks for container image tags."""

import base64
import contextlib
import fcntl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from bro.workspace.paths import runtime_base

_LOCK_DIRECTORY = 'image-locks'
_ENCODED_TAG_COMPONENT_LENGTH = 128
_RESERVATIONS_GUARD = threading.Lock()
_RESERVATIONS: dict[Path, BinaryIO] = {}
_RESERVATION_LOCKS: dict[Path, threading.Lock] = {}


def _encoded_tag(tag: str) -> str:
  if tag == '':
    raise ValueError('image tag must not be empty')
  return base64.urlsafe_b64encode(tag.encode()).decode('ascii').rstrip('=')


def _tag_lock_directory(tag: str) -> Path:
  encoded_tag = _encoded_tag(tag)
  path = runtime_base() / _LOCK_DIRECTORY
  for offset in range(0, len(encoded_tag), _ENCODED_TAG_COMPONENT_LENGTH):
    path /= encoded_tag[offset : offset + _ENCODED_TAG_COMPONENT_LENGTH]
  return path


def image_lock_path(tag: str) -> Path:
  return _tag_lock_directory(tag) / 'tag.lock'


def build_lock_path(tag: str) -> Path:
  return _tag_lock_directory(tag) / 'build.lock'


def _reservation_lock(path: Path) -> threading.Lock:
  with _RESERVATIONS_GUARD:
    return _RESERVATION_LOCKS.setdefault(path, threading.Lock())


def reserve_image(tag: str) -> None:
  """Hold a shared lock for this process's remaining lifetime."""
  path = image_lock_path(tag)
  with _reservation_lock(path):
    with _RESERVATIONS_GUARD:
      if path in _RESERVATIONS:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as cleanup:
      handle = cleanup.enter_context(path.open('a+b'))
      fcntl.flock(handle, fcntl.LOCK_SH)
      with _RESERVATIONS_GUARD:
        _RESERVATIONS[path] = handle
      cleanup.pop_all()


@contextlib.contextmanager
def image_build_lock(tag: str) -> Iterator[None]:
  """Serialize builders of one image tag across host processes."""
  path = build_lock_path(tag)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('a+b') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    yield


@contextlib.contextmanager
def ensure_image(tag: str) -> Iterator[None]:
  """Reserve an image for this process and serialize its ensure operation."""
  reserve_image(tag)
  with image_build_lock(tag):
    yield


@contextlib.contextmanager
def image_removal_lock(tag: str) -> Iterator[bool]:
  """Yield whether this process exclusively reserved a tag for removal."""
  path = image_lock_path(tag)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('a+b') as handle:
    try:
      fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      yield False
    else:
      yield True
