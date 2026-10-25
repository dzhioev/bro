"""PDF inspection in the file worker."""

from typing import Any, Literal

from bro.files.pages import select_pages
from bro.files.protocol import InvalidInput
from bro.files.types import Limits
from bro.pdf import TYPE, Details, PageText, Unreadable
from bro.pdf.attachments import attachments
from bro.pdf.engine import Engine
from bro.pdf.links import page_links
from bro.pdf.page_text import text_and_notes
from bro.pdf.view_limits import ATTACHMENT_NAMES, NAME_CHARACTERS, ViewBudget

TITLE_CHARACTERS = 200
PAGE_NUMBERS_WITHOUT_TEXT = 100


class PdfProvider:
  def __init__(self):
    self._engine: Engine | None = None

  @property
  def engine(self) -> Engine:
    if self._engine is None:
      raise RuntimeError('PDF provider has not been prepared')
    return self._engine

  def prepare(self) -> str:
    if self._engine is None:
      self._engine = Engine()
      self._engine.warmup(TYPE.limits)
    return self.engine.version

  def info(self, snapshot: bytes, limits: Limits) -> dict[str, Any]:
    engine = self.engine
    with engine.document(snapshot, limits) as document:
      if document is None:
        return Details(
          page_count=None,
          title=None,
          password_required=True,
          form=None,
          attachment_count=None,
          attachments=None,
          text_layer=None,
          page_numbers_without_text=None,
          without_text_page_count=None,
        ).model_dump()
      title = engine.wide_string(engine.metadata, document.handle, b'Title')
      forms: dict[int, Literal['none', 'acroform', 'xfa']] = {
        0: 'none',
        1: 'acroform',
        2: 'xfa',
        3: 'xfa',
      }
      form = forms[engine.form_type(document.handle)]
      count = engine.attachment_count(document.handle)
      if count < 0:
        raise RuntimeError('PDFium failed to count attachments')
      names = []
      for index in range(min(count, ATTACHMENT_NAMES)):
        attachment = engine.attachment(document.handle, index)
        if not attachment:
          raise RuntimeError('PDFium failed to load an attachment')
        names.append(engine.wide_string(engine.attachment_name, attachment)[:NAME_CHARACTERS])
      without_text = []
      for index in range(document.page_count):
        with document.page(index) as page:
          if page is None or not page.has_text():
            without_text.append(index + 1)
      return Details(
        page_count=document.page_count,
        title=title[:TITLE_CHARACTERS] if title else None,
        password_required=False,
        form=form,
        attachment_count=count,
        attachments=names,
        text_layer=(
          'none'
          if len(without_text) == document.page_count
          else 'partial'
          if without_text
          else 'full'
        ),
        page_numbers_without_text=without_text[:PAGE_NUMBERS_WITHOUT_TEXT],
        without_text_page_count=len(without_text),
      ).model_dump()

  def view(
    self, snapshot: bytes, view: str, pages: list[int] | None, limits: Limits
  ) -> dict[str, Any]:
    with self.engine.document(snapshot, limits) as document:
      if document is None:
        raise InvalidInput('PDF requires a password; provide a PDF without a user password')
      if view == 'attachments':
        if pages is not None:
          raise InvalidInput('PDF attachments view takes no pages; omit pages')
        return attachments(document, limits)
      if view not in ('text', 'links'):
        raise InvalidInput('unknown PDF view; use a declared view')
      if limits.max_pages is None:
        raise RuntimeError('PDF limits must declare max_pages')
      try:
        selected = select_pages(pages, page_count=document.page_count, max_pages=limits.max_pages)
      except ValueError as error:
        raise InvalidInput(str(error)) from error
      budget = ViewBudget(limits)
      result = []
      for number in selected:
        with document.page(number - 1) as page:
          if view == 'links':
            if page is not None:
              result.extend(page_links(document, page, number, budget, len(result)))
            continue
          if page is None:
            entry = PageText(page_number=number, notes=[Unreadable()], text='')
          else:
            text, notes = text_and_notes(page.classified_characters())
            entry = PageText(page_number=number, notes=notes, text=text)
          value = entry.model_dump()
          budget.add(value, location=f'page {number}')
          result.append(value)
      return {'links' if view == 'links' else 'pages': result}


provider = PdfProvider()
