"""PDF file-type declaration; the engine is loaded only by the provider."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from bro.files import FileType, Limits, Signature


class Details(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  page_count: int | None
  title: str | None
  password_required: bool
  form: Literal['none', 'acroform', 'xfa'] | None
  attachment_count: int | None
  attachments: list[str] | None
  text_layer: Literal['full', 'partial', 'none'] | None
  page_numbers_without_text: list[int] | None
  without_text_page_count: int | None


TYPE = FileType(
  name='pdf',
  signatures=(Signature(b'%PDF-', within=1024),),
  views=(),
  details=Details,
  provider='bro.pdf.provider:provider',
  limits=Limits(
    max_bytes=32 * 1024**2,
    max_pages=5000,
    max_view_characters=1000000,
    max_view_entries=20000,
  ),
  manual=Path(__file__).with_name('manual.md'),
)
