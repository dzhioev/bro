"""Declared-text attachment inspection without opening other embedded streams."""

import ctypes
from typing import Any

from bro.files.text import sanitize
from bro.files.types import Limits
from bro.pdf import Attachment
from bro.pdf.engine import Document
from bro.pdf.view_limits import ATTACHMENT_NAMES, NAME_CHARACTERS, ViewBudget

TEXT_BYTES = 256 * 1024


def is_text(name: str, media_type: str | None) -> bool:
  if media_type is None:
    return name.lower().endswith(('.xml', '.txt', '.json', '.csv'))
  media_type = media_type.lower()
  return (
    media_type.startswith('text/')
    or media_type in ('application/xml', 'application/json')
    or media_type.endswith(('+xml', '+json'))
  )


def read_attachment(
  document: Document, handle: int, name: str, media_type: str | None
) -> Attachment:
  entry = Attachment(
    name=name[:NAME_CHARACTERS], media_type=media_type, size_bytes=None, text=None, not_shown=None
  )
  if not is_text(name, media_type):
    entry.not_shown = 'not a declared text type'
    return entry
  engine = document.engine
  length = ctypes.c_ulong()
  if not engine.attachment_file(handle, None, 0, ctypes.byref(length)):
    entry.not_shown = 'no embedded file'
    return entry
  entry.size_bytes = length.value
  if length.value > TEXT_BYTES:
    entry.not_shown = f'larger than {TEXT_BYTES // 1024} KiB'
    return entry
  buffer = ctypes.create_string_buffer(length.value)
  # PDFium can trap on a short buffer and silently copy nothing on a long one.
  if not engine.attachment_file(handle, buffer, entry.size_bytes, ctypes.byref(length)):
    raise RuntimeError('PDFium failed to read an embedded file')
  if length.value != entry.size_bytes:
    raise RuntimeError('PDFium returned an inconsistent embedded-file length')
  try:
    text = buffer.raw.decode('utf-8', errors='strict')
  except UnicodeDecodeError:
    entry.not_shown = 'not valid text'
    return entry
  if any(
    (ord(character) < 32 and character not in '\t\n\r') or 127 <= ord(character) <= 159
    for character in text
  ):
    entry.not_shown = 'not valid text'
    return entry
  entry.text = sanitize(text).text
  return entry


def attachments(document: Document, limits: Limits) -> dict[str, Any]:
  engine = document.engine
  count = engine.attachment_count(document.handle)
  if count < 0:
    raise RuntimeError('PDFium failed to count attachments')
  budget = ViewBudget(limits, paged=False)
  entries = []
  for index in range(min(count, ATTACHMENT_NAMES)):
    handle = engine.attachment(document.handle, index)
    if not handle:
      raise RuntimeError('PDFium failed to load an attachment')
    name = engine.wide_string(engine.attachment_name, handle)
    media_type = engine.wide_string(engine.attachment_subtype, handle) or None
    entry = read_attachment(document, handle, name, media_type).model_dump()
    budget.add(entry, location=f'attachment {index + 1}')
    entries.append(entry)
  return {'attachment_count': count, 'attachments': entries}
