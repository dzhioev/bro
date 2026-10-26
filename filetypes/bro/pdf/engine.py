"""Direct PDFium bindings and scoped handles."""

import ctypes
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from math import hypot
from pathlib import Path
from typing import Any

import pypdfium2_raw as raw
from pypdfium2_raw.version import PDFIUM_INFO

from bro.files.protocol import InvalidInput
from bro.files.text import sanitize
from bro.files.types import Limits
from bro.pdf import HiddenReason
from bro.pdf.page_text import Character, assemble

_POINTER = ctypes.c_void_p
_INTEGER = ctypes.c_int
_UNSIGNED_LONG = ctypes.c_ulong

_WARMUP = b"""%PDF-1.7
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj
4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
5 0 obj << /Length 43 >> stream
BT /F1 12 Tf 72 720 Td (PDFium warmup) Tj ET
endstream endobj
trailer << /Root 1 0 R >>
%%EOF
"""


def clean_string(text: str) -> str:
  return sanitize(
    ''.join(
      '\ufffd'
      if (ord(character) < 32 and character not in '\t\n\r')
      or 127 <= ord(character) <= 159
      or 0xD800 <= ord(character) <= 0xDFFF
      else character
      for character in text
    )
  ).text


class Engine:
  def __init__(self):
    library_name = {
      'win32': 'pdfium.dll',
      'darwin': 'libpdfium.dylib',
    }.get(sys.platform, 'libpdfium.so')
    self.library = ctypes.CDLL(str(Path(raw.__file__).with_name(library_name)))
    self.initialize = self.bind('FPDF_InitLibrary', None)
    self.set_fonts = self.bind('FPDF_SetSystemFontInfo', None, _POINTER)
    self.load_document = self.bind(
      'FPDF_LoadMemDocument64', _POINTER, _POINTER, ctypes.c_size_t, ctypes.c_char_p
    )
    self.last_error = self.bind('FPDF_GetLastError', _UNSIGNED_LONG)
    self.close_document = self.bind('FPDF_CloseDocument', None, _POINTER)
    self.page_count = self.bind('FPDF_GetPageCount', _INTEGER, _POINTER)
    self.load_page = self.bind('FPDF_LoadPage', _POINTER, _POINTER, _INTEGER)
    self.close_page = self.bind('FPDF_ClosePage', None, _POINTER)
    self.load_text = self.bind('FPDFText_LoadPage', _POINTER, _POINTER)
    self.close_text = self.bind('FPDFText_ClosePage', None, _POINTER)
    self.character_count = self.bind('FPDFText_CountChars', _INTEGER, _POINTER)
    self.unicode = self.bind('FPDFText_GetUnicode', ctypes.c_uint, _POINTER, _INTEGER)
    self.generated = self.bind('FPDFText_IsGenerated', _INTEGER, _POINTER, _INTEGER)
    self.hyphen = self.bind('FPDFText_IsHyphen', _INTEGER, _POINTER, _INTEGER)
    self.bulk_text = self.bind('FPDFText_GetText', _INTEGER, _POINTER, _INTEGER, _INTEGER, _POINTER)
    self.text_object = self.bind('FPDFText_GetTextObject', _POINTER, _POINTER, _INTEGER)
    self.render_mode = self.bind('FPDFTextObj_GetTextRenderMode', _INTEGER, _POINTER)
    self.font_size = self.bind('FPDFText_GetFontSize', ctypes.c_double, _POINTER, _INTEGER)
    self.character_matrix = self.bind('FPDFText_GetMatrix', _INTEGER, _POINTER, _INTEGER, _POINTER)
    self.character_box = self.bind(
      'FPDFText_GetCharBox', _INTEGER, _POINTER, _INTEGER, _POINTER, _POINTER, _POINTER, _POINTER
    )
    self.page_box = self.bind('FPDF_GetPageBoundingBox', _INTEGER, _POINTER, _POINTER)
    self.fill_color = self.bind(
      'FPDFPageObj_GetFillColor', _INTEGER, _POINTER, _POINTER, _POINTER, _POINTER, _POINTER
    )
    self.stroke_color = self.bind(
      'FPDFPageObj_GetStrokeColor', _INTEGER, _POINTER, _POINTER, _POINTER, _POINTER, _POINTER
    )
    self.mark_count = self.bind('FPDFPageObj_CountMarks', _INTEGER, _POINTER)
    self.mark = self.bind('FPDFPageObj_GetMark', _POINTER, _POINTER, _UNSIGNED_LONG)
    self.mark_name = self.bind(
      'FPDFPageObjMark_GetName', _INTEGER, _POINTER, _POINTER, _UNSIGNED_LONG, _POINTER
    )
    self.unmapped = self.bind('FPDFText_HasUnicodeMapError', _INTEGER, _POINTER, _INTEGER)
    self.metadata = self.bind(
      'FPDF_GetMetaText', _UNSIGNED_LONG, _POINTER, ctypes.c_char_p, _POINTER, _UNSIGNED_LONG
    )
    self.form_type = self.bind('FPDF_GetFormType', _INTEGER, _POINTER)
    self.initialize_forms = self.bind(
      'FPDFDOC_InitFormFillEnvironment', _POINTER, _POINTER, _POINTER
    )
    self.close_forms = self.bind('FPDFDOC_ExitFormFillEnvironment', None, _POINTER)
    self.after_load_page = self.bind('FORM_OnAfterLoadPage', None, _POINTER, _POINTER)
    self.before_close_page = self.bind('FORM_OnBeforeClosePage', None, _POINTER, _POINTER)
    self.annotation_count = self.bind('FPDFPage_GetAnnotCount', _INTEGER, _POINTER)
    self.annotation = self.bind('FPDFPage_GetAnnot', _POINTER, _POINTER, _INTEGER)
    self.close_annotation = self.bind('FPDFPage_CloseAnnot', None, _POINTER)
    self.annotation_subtype = self.bind('FPDFAnnot_GetSubtype', _INTEGER, _POINTER)
    self.annotation_rectangle = self.bind('FPDFAnnot_GetRect', _INTEGER, _POINTER, _POINTER)
    self.annotation_flags = self.bind('FPDFAnnot_GetFlags', _INTEGER, _POINTER)
    self.field_type = self.bind('FPDFAnnot_GetFormFieldType', _INTEGER, _POINTER, _POINTER)
    self.field_flags = self.bind('FPDFAnnot_GetFormFieldFlags', _INTEGER, _POINTER, _POINTER)
    self.field_name = self.bind(
      'FPDFAnnot_GetFormFieldName', _UNSIGNED_LONG, _POINTER, _POINTER, _POINTER, _UNSIGNED_LONG
    )
    self.field_value = self.bind(
      'FPDFAnnot_GetFormFieldValue', _UNSIGNED_LONG, _POINTER, _POINTER, _POINTER, _UNSIGNED_LONG
    )
    self.field_checked = self.bind('FPDFAnnot_IsChecked', _INTEGER, _POINTER, _POINTER)
    self.option_count = self.bind('FPDFAnnot_GetOptionCount', _INTEGER, _POINTER, _POINTER)
    self.option_label = self.bind(
      'FPDFAnnot_GetOptionLabel',
      _UNSIGNED_LONG,
      _POINTER,
      _POINTER,
      _INTEGER,
      _POINTER,
      _UNSIGNED_LONG,
    )
    self.option_selected = self.bind(
      'FPDFAnnot_IsOptionSelected', _INTEGER, _POINTER, _POINTER, _INTEGER
    )
    self.attachment_count = self.bind('FPDFDoc_GetAttachmentCount', _INTEGER, _POINTER)
    self.attachment = self.bind('FPDFDoc_GetAttachment', _POINTER, _POINTER, _INTEGER)
    self.attachment_name = self.bind(
      'FPDFAttachment_GetName', _UNSIGNED_LONG, _POINTER, _POINTER, _UNSIGNED_LONG
    )
    self.attachment_subtype = self.bind(
      'FPDFAttachment_GetSubtype', _UNSIGNED_LONG, _POINTER, _POINTER, _UNSIGNED_LONG
    )
    self.attachment_file = self.bind(
      'FPDFAttachment_GetFile', _INTEGER, _POINTER, _POINTER, _UNSIGNED_LONG, _POINTER
    )
    self.enumerate_link = self.bind('FPDFLink_Enumerate', _INTEGER, _POINTER, _POINTER, _POINTER)
    self.link_rectangle = self.bind('FPDFLink_GetAnnotRect', _INTEGER, _POINTER, _POINTER)
    self.link_annotation = self.bind('FPDFLink_GetAnnot', _POINTER, _POINTER, _POINTER)
    self.link_action = self.bind('FPDFLink_GetAction', _POINTER, _POINTER)
    self.action_type = self.bind('FPDFAction_GetType', _UNSIGNED_LONG, _POINTER)
    self.action_uri = self.bind(
      'FPDFAction_GetURIPath', _UNSIGNED_LONG, _POINTER, _POINTER, _POINTER, _UNSIGNED_LONG
    )
    if PDFIUM_INFO.flags:
      raise RuntimeError('PDFium must be built without optional features')
    self.initialize()
    self.fonts = raw.FPDF_SYSFONTINFO(version=1)
    self.set_fonts(ctypes.byref(self.fonts))

  def bind(self, name: str, result: Any, *arguments: Any) -> Any:
    return ctypes.CFUNCTYPE(result, *arguments)((name, self.library))

  @property
  def version(self) -> str:
    return str(PDFIUM_INFO)

  @contextmanager
  def document(self, snapshot: bytes, limits: Limits):
    handle = self.load_document(snapshot, len(snapshot), None)
    if not handle:
      error = self.last_error()
      if error == 4:
        yield None
        return
      if error == 3:
        raise InvalidInput('not a readable PDF; provide an intact PDF')
      if error == 5:
        raise InvalidInput('unsupported PDF security handler; provide an unencrypted PDF')
      raise RuntimeError(f'PDFium failed to open a document: error {error}')
    try:
      count = self.page_count(handle)
      if count <= 0:
        raise InvalidInput('PDF has no pages; provide a PDF with pages')
      if limits.max_pages is None:
        raise RuntimeError('PDF limits must declare max_pages')
      if count > limits.max_pages:
        raise InvalidInput(
          f'PDF has {count} pages, above the limit of {limits.max_pages}; split the document'
        )
      yield Document(self, handle, count)
    finally:
      self.close_document(handle)

  def wide_string(self, function: Any, *arguments: Any) -> str:
    size = function(*arguments, None, 0)
    if size == 0:
      return ''
    if size < 2 or size % 2:
      raise RuntimeError('PDFium returned an invalid UTF-16 buffer size')
    buffer = ctypes.create_string_buffer(size)
    if function(*arguments, buffer, size) != size:
      raise RuntimeError('PDFium returned an inconsistent UTF-16 buffer size')
    return clean_string(buffer.raw[:-2].decode('utf-16-le', errors='replace'))

  def warmup(self, limits: Limits) -> None:
    with self.document(_WARMUP, limits) as document:
      if document is None:
        raise RuntimeError('PDFium could not open its built-in document')
      self.wide_string(self.metadata, document.handle, b'Title')
      self.form_type(document.handle)
      self.attachment_count(document.handle)
      with document.page(0) as page:
        if page is None or not page.has_text():
          raise RuntimeError('PDFium could not read its built-in document')
        page.classified_characters()


@dataclass
class Document:
  engine: Engine
  handle: int
  page_count: int

  @contextmanager
  def form_environment(self):
    info = raw.FPDF_FORMFILLINFO(version=2)
    environment = self.engine.initialize_forms(self.handle, ctypes.byref(info))
    if not environment:
      raise RuntimeError('PDFium failed to initialize a form environment')
    try:
      yield environment
    finally:
      self.engine.close_forms(environment)

  @contextmanager
  def form_page(self, index: int, environment: int):
    with self.page(index) as page:
      if page is None:
        yield None
        return
      self.engine.after_load_page(page.handle, environment)
      try:
        yield page
      finally:
        self.engine.before_close_page(page.handle, environment)

  @contextmanager
  def page(self, index: int):
    page = self.engine.load_page(self.handle, index)
    if not page:
      yield None
      return
    try:
      text = self.engine.load_text(page)
      if not text:
        raise RuntimeError('PDFium failed to load a text page')
      try:
        yield TextPage(self.engine, page, text)
      finally:
        self.engine.close_text(text)
    finally:
      self.engine.close_page(page)


@dataclass
class TextPage:
  engine: Engine
  handle: int
  text_handle: int

  @contextmanager
  def annotation(self, index: int):
    handle = self.engine.annotation(self.handle, index)
    if not handle:
      yield None
      return
    try:
      yield handle
    finally:
      self.engine.close_annotation(handle)

  def count(self) -> int:
    count = self.engine.character_count(self.text_handle)
    if count < 0:
      raise RuntimeError('PDFium failed to count page characters')
    return count

  def characters(self) -> list[Character]:
    unicode = self.engine.unicode
    generated = self.engine.generated
    hyphen = self.engine.hyphen
    text = self.text_handle
    characters = []
    for index in range(self.count()):
      code = unicode(text, index)
      characters.append(
        Character(code, bool(generated(text, index)), code == 2 and bool(hyphen(text, index)))
      )
    return characters

  def classified_characters(self) -> list[Character]:
    engine = self.engine
    text = self.text_handle
    bounds = raw.FS_RECTF()
    if not engine.page_box(self.handle, ctypes.byref(bounds)):
      raise RuntimeError('PDFium failed to read a page box')
    left, right, bottom, top = (ctypes.c_double() for _ in range(4))
    box_pointers = tuple(ctypes.addressof(value) for value in (left, right, bottom, top))
    unicode = engine.unicode
    is_generated = engine.generated
    is_hyphen = engine.hyphen
    character_box = engine.character_box
    text_object = engine.text_object
    unmapped = engine.unmapped
    styles: dict[int, TextStyle] = {}
    characters = []
    for index in range(self.count()):
      code = unicode(text, index)
      generated = bool(is_generated(text, index))
      hyphen = code == 2 and bool(is_hyphen(text, index))
      if not character_box(text, index, *box_pointers):
        raise RuntimeError('PDFium failed to read a character box')
      box = (left.value, right.value, bottom.value, top.value)
      if generated:
        characters.append(Character(code, generated, hyphen, box=box))
        continue
      handle = text_object(text, index)
      if not handle:
        raise RuntimeError('PDFium returned no text object for a non-generated character')
      style = styles.get(handle)
      if style is None:
        style = self.style(handle, index)
        styles[handle] = style
      reason: HiddenReason | None = None
      if style.invisible:
        reason = 'invisible'
      elif (
        right.value < bounds.left
        or left.value > bounds.right
        or top.value < bounds.bottom
        or bottom.value > bounds.top
      ):
        reason = 'outside_page'
      elif style.effective_size < 2:
        reason = 'tiny'
      elif style.white:
        reason = 'white'
      elif style.layer:
        reason = 'layer'
      characters.append(Character(code, False, hyphen, reason, bool(unmapped(text, index)), box))
    return characters

  def style(self, handle: int, index: int) -> 'TextStyle':
    engine = self.engine
    mode = engine.render_mode(handle)
    if mode not in range(8):
      raise RuntimeError('PDFium returned an invalid text render mode')
    colors = []
    red, green, blue, alpha = (ctypes.c_uint() for _ in range(4))
    pointers = tuple(ctypes.byref(value) for value in (red, green, blue, alpha))
    for function in (engine.fill_color, engine.stroke_color):
      if not function(handle, *pointers):
        raise RuntimeError('PDFium failed to read a text color')
      colors.append((red.value, green.value, blue.value, alpha.value))
    painted = [colors[0]] if mode in (0, 4) else [colors[1]] if mode in (1, 5) else colors
    invisible = mode in (3, 7) or all(color[3] == 0 for color in painted)
    white = all(color[3] == 0 or min(color[:3]) >= 250 for color in painted)
    count = engine.mark_count(handle)
    if count < 0:
      raise RuntimeError('PDFium failed to count text-object marks')
    layer = False
    for mark_index in range(count):
      mark = engine.mark(handle, mark_index)
      if not mark:
        raise RuntimeError('PDFium failed to read a text-object mark')
      size = ctypes.c_ulong()
      if (
        not engine.mark_name(mark, None, 0, ctypes.byref(size)) or size.value < 2 or size.value % 2
      ):
        raise RuntimeError('PDFium failed to read a text-object mark name')
      buffer = ctypes.create_string_buffer(size.value)
      if not engine.mark_name(mark, buffer, len(buffer), ctypes.byref(size)):
        raise RuntimeError('PDFium failed to read a text-object mark name')
      layer |= buffer.raw[:-2].decode('utf-16-le', errors='replace') == 'OC'
    matrix = raw.FS_MATRIX()
    if not engine.character_matrix(self.text_handle, index, ctypes.byref(matrix)):
      raise RuntimeError('PDFium failed to read a character matrix')
    effective_size = abs(engine.font_size(self.text_handle, index)) * hypot(matrix.c, matrix.d)
    return TextStyle(effective_size, invisible, white, layer)

  def has_text(self) -> bool:
    count = self.count()
    if count == 0:
      return False
    buffer = (ctypes.c_ushort * (count + 1))()
    written = self.engine.bulk_text(self.text_handle, 0, count, buffer)
    if written <= 0 or written > count + 1:
      raise RuntimeError('PDFium returned an invalid page-text length')
    bulk = bytes(buffer)[: (written - 1) * 2].decode('utf-16-le', errors='replace')
    if clean_string(bulk).strip():
      return True
    return bool(assemble(self.characters()).strip())


@dataclass(frozen=True)
class TextStyle:
  effective_size: float
  invisible: bool
  white: bool
  layer: bool
