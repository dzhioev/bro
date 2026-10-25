"""Bounded off-loop snapshots and abandoned-read ownership."""

import asyncio
import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base.offload import off_loop
from bro.files import access
from bro.files.registry import TypeRegistry
from bro.files.slots import SlotLease
from bro.files.types import IDENTIFICATION_BYTES, FileType


@dataclass(frozen=True)
class Snapshot:
  file_type: FileType
  data: bytes
  sha256: str


def _read(path: Path, type: Optional[str], registry: TypeRegistry) -> Snapshot:
  file_type = registry.get(type) if type is not None else None
  try:
    with access.current().open_read(path) as stream:
      prefix_bytes = (
        min(IDENTIFICATION_BYTES, file_type.limits.max_bytes + 1)
        if file_type
        else IDENTIFICATION_BYTES
      )
      data = bytearray()
      digest = hashlib.sha256()
      while len(data) < prefix_bytes:
        chunk = stream.read(prefix_bytes - len(data))
        if not chunk:
          break
        data.extend(chunk)
        digest.update(chunk)
      if file_type is None:
        file_type = registry.identify(bytes(data))
      maximum = file_type.limits.max_bytes
      while len(data) <= maximum:
        chunk = stream.read(min(65536, maximum + 1 - len(data)))
        if not chunk:
          return Snapshot(file_type, bytes(data), digest.hexdigest())
        data.extend(chunk)
        digest.update(chunk)
      raise ValueError(
        f'file {str(path)!r} has at least {len(data)} bytes, above the limit of '
        f'{maximum} bytes for {file_type.name}; use a smaller file'
      )
  except access.FileAccessDenied:
    raise
  except OSError as error:
    raise ValueError(
      f'cannot read file {str(path)!r}: {error}; provide a readable regular-file path'
    ) from error


class SnapshotRead:
  def __init__(self, lease: SlotLease):
    self._lease = lease
    self._lock = threading.Lock()
    self._finished = False
    self._abandoned = False

  @contextmanager
  def _completion(self) -> Iterator[None]:
    try:
      yield
    finally:
      with self._lock:
        self._finished = True
        if self._abandoned:
          self._lease.release()

  def _run(self, path: Path, type: Optional[str], registry: TypeRegistry) -> Snapshot:
    with self._completion():
      return _read(path, type, registry)

  async def read(self, path: Path, type: Optional[str], registry: TypeRegistry) -> Snapshot:
    try:
      return await off_loop(self._run, path, type, registry)
    except asyncio.CancelledError:
      with self._lock:
        if not self._finished:
          self._lease.transfer()
          self._abandoned = True
      raise
