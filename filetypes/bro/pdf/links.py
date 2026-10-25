"""PDF link annotations and visible anchor selection."""

import ctypes
from bisect import bisect_left, bisect_right
from contextlib import ExitStack
from typing import Any

import pypdfium2_raw as raw

from bro.pdf import Link
from bro.pdf.engine import Document, TextPage
from bro.pdf.page_text import Character, assemble
from bro.pdf.view_limits import ViewBudget

ANCHOR_CHARACTERS = 200


def normalized(rectangle: Any) -> tuple[float, float, float, float]:
  return (
    min(rectangle.left, rectangle.right),
    max(rectangle.left, rectangle.right),
    min(rectangle.bottom, rectangle.top),
    max(rectangle.bottom, rectangle.top),
  )


class Anchors:
  def __init__(self, characters: list[Character]):
    self.characters = characters
    self.centres = sorted(
      ((character.box[0] + character.box[1]) / 2, index)
      for index, character in enumerate(characters)
      if character.reason is None and character.box is not None
    )

  def text(self, rectangle: tuple[float, float, float, float]) -> str:
    left, right, bottom, top = rectangle
    start = bisect_left(self.centres, left, key=lambda item: item[0])
    end = bisect_right(self.centres, right, key=lambda item: item[0])
    indexes = []
    for _, index in self.centres[start:end]:
      box = self.characters[index].box
      assert box is not None
      if bottom <= (box[2] + box[3]) / 2 <= top:
        indexes.append(index)
    return ' '.join(assemble(self.characters[index] for index in sorted(indexes)).split())[
      :ANCHOR_CHARACTERS
    ]


def uri(document: Document, link: int) -> str | None:
  engine = document.engine
  action = engine.link_action(link)
  if not action or engine.action_type(action) != 3:
    return None
  size = engine.action_uri(document.handle, action, None, 0)
  if size == 0:
    return ''
  buffer = ctypes.create_string_buffer(size)
  if engine.action_uri(document.handle, action, buffer, size) != size:
    raise RuntimeError('PDFium returned an inconsistent URI buffer size')
  return ''.join(chr(byte) if 32 <= byte <= 126 else f'%{byte:02X}' for byte in buffer.raw[:-1])


def page_links(
  document: Document, page: TextPage, number: int, budget: ViewBudget, offset: int
) -> list[dict[str, Any]]:
  engine = document.engine
  bounds = raw.FS_RECTF()
  if not engine.page_box(page.handle, ctypes.byref(bounds)):
    raise RuntimeError('PDFium failed to read a page box')
  page_left, page_right, page_bottom, page_top = normalized(bounds)
  anchors: Anchors | None = None
  position = ctypes.c_int()
  link = ctypes.c_void_p()
  entries = []
  while engine.enumerate_link(page.handle, ctypes.byref(position), ctypes.byref(link)):
    if not link.value:
      raise RuntimeError('PDFium returned no enumerated link')
    rectangle = raw.FS_RECTF()
    if not engine.link_rectangle(link, ctypes.byref(rectangle)):
      raise RuntimeError('PDFium failed to read a link rectangle')
    left, right, bottom, top = normalized(rectangle)
    with ExitStack() as scope:
      annotation = engine.link_annotation(page.handle, link)
      if not annotation:
        raise RuntimeError('PDFium failed to load a link annotation')
      scope.callback(engine.close_annotation, annotation)
      flags = engine.annotation_flags(annotation)
      if flags < 0:
        raise RuntimeError('PDFium failed to read annotation flags')
    if anchors is None:
      anchors = Anchors(page.classified_characters())
    entry = Link(
      page_number=number,
      hidden=bool(flags & (2 | 32))
      or left == right
      or bottom == top
      or right < page_left
      or left > page_right
      or top < page_bottom
      or bottom > page_top,
      text=anchors.text((left, right, bottom, top)),
      uri=uri(document, link.value),
    ).model_dump()
    budget.add(entry, location=f'link {offset + len(entries) + 1} on page {number}')
    entries.append(entry)
  return entries
