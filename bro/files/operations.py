"""Parser-free file operations."""

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from bro.files.models import InfoResult, ViewResult
from bro.files.pages import parse_pages
from bro.files.protocol import Job, Reply
from bro.files.registry import installed_type_names, registry
from bro.files.runner import Runner, SubprocessRunner
from bro.files.slots import READ_SECONDS, SlotLease, WorkerSlots, default_slots
from bro.files.snapshots import Snapshot, SnapshotRead
from bro.files.text import sanitize, validate_strings
from bro.files.types import FileType


def _field_names(schema: Any) -> set[str]:
  if isinstance(schema, dict):
    names = set(schema.get('properties', {}))
    for child in schema.values():
      names.update(_field_names(child))
    return names
  if isinstance(schema, list):
    return set().union(*(_field_names(child) for child in schema))
  return set()


def validate_manual(file_type: FileType, text: str) -> None:
  models = [file_type.details, *(view.content for view in file_type.views)]
  fields = set().union(
    *(
      _field_names(model.model_json_schema(by_alias=False, mode='serialization'))
      for model in models
    )
  )
  missing = sorted(
    field for field in fields if re.search(rf'(?<!\w){re.escape(field)}(?!\w)', text) is None
  )
  if missing:
    raise ValueError(f'file type {file_type.name!r} manual omits fields: {", ".join(missing)}')


def read_reference(type: Optional[str] = None) -> str:
  if type is not None:
    file_type = registry.get(type)
    text = file_type.manual.read_text(encoding='utf-8')
    text = sanitize(text).text
    validate_strings(text)
    validate_manual(file_type, text)
    return text
  shared = Path(__file__).with_name('reference.md').read_text(encoding='utf-8')
  names = installed_type_names()
  installed = '\n'.join(f'- {name}' for name in names) if names else '(none)'
  return shared.rstrip() + '\n\n## Installed file types\n\n' + installed + '\n'


class Operations:
  def __init__(
    self,
    runner: Runner | None = None,
    *,
    slots: WorkerSlots | None = None,
    read_seconds: float = READ_SECONDS,
  ):
    if not 0 < read_seconds < float('inf'):
      raise ValueError('read_seconds must be finite and positive')
    self._runner = runner
    self._slots = slots if slots is not None else default_slots
    self._read_seconds = read_seconds
    self._closed = False

  def close(self) -> None:
    self._closed = True
    if isinstance(self._runner, SubprocessRunner):
      self._runner.close()

  async def _snapshot(self, path: str, type: Optional[str], lease: SlotLease) -> Snapshot:
    if self._closed:
      raise RuntimeError('file operations are closed')
    try:
      async with asyncio.timeout(self._read_seconds):
        return await SnapshotRead(lease).read(Path(path), type, registry)
    except TimeoutError:
      raise RuntimeError(f'file read timed out for {path!r}')

  async def _run(self, job: Job) -> Reply:
    if self._closed:
      raise RuntimeError('file operations are closed')
    if self._runner is None:
      self._runner = SubprocessRunner(self._slots.limits)
    return await self._runner.run(job)

  async def info(self, path: str, type: Optional[str] = None) -> InfoResult:
    with await self._slots.acquire() as lease:
      snapshot = await self._snapshot(path, type, lease)
      reply = await self._run(Job(snapshot.file_type, 'info', {}, snapshot.data))
      return InfoResult.from_validated(
        snapshot.file_type,
        size_bytes=len(snapshot.data),
        sha256=snapshot.sha256,
        details=reply.ok,
      )

  async def view(
    self, path: str, view: str, pages: Optional[str] = None, type: Optional[str] = None
  ) -> ViewResult:
    with await self._slots.acquire() as lease:
      snapshot = await self._snapshot(path, type, lease)
      file_type = snapshot.file_type
      declaration = file_type.get_view(view)
      selection = parse_pages(pages, max_pages=file_type.limits.max_pages, paged=declaration.paged)
      reply = await self._run(
        Job(file_type, 'view', {'view': view, 'pages': selection}, snapshot.data)
      )
      return ViewResult.from_validated(
        file_type, sha256=snapshot.sha256, view=view, content=reply.ok
      )


_default = Operations()


async def info(path: str, type: Optional[str] = None) -> InfoResult:
  return await _default.info(path, type)


async def view(
  path: str, view: str, pages: Optional[str] = None, type: Optional[str] = None
) -> ViewResult:
  return await _default.view(path, view, pages, type)
