"""PDF file-type declaration; the engine is loaded only by the provider."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bro.files import FileType, Limits, Signature, View


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


HiddenReason = Literal['invisible', 'outside_page', 'tiny', 'white', 'layer', 'tag_characters']


class HiddenText(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  note: Literal['hidden_text'] = 'hidden_text'
  reason: HiddenReason
  text: str


class HiddenTextMore(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  note: Literal['hidden_text_more'] = 'hidden_text_more'
  run_count: int = Field(gt=0)


class UnmappedCharacters(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  note: Literal['unmapped_characters'] = 'unmapped_characters'
  character_count: int = Field(gt=0)


class Unreadable(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  note: Literal['unreadable'] = 'unreadable'


TextNote = HiddenText | HiddenTextMore | UnmappedCharacters | Unreadable


class PageText(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  page_number: int = Field(gt=0)
  notes: list[TextNote]
  text: str


class TextContent(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  pages: list[PageText]


class Link(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  page_number: int = Field(gt=0)
  hidden: bool
  text: str
  uri: str | None


class LinksContent(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  links: list[Link]


class Attachment(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  name: str
  media_type: str | None
  size_bytes: int | None = Field(ge=0)
  text: str | None
  not_shown: str | None


class AttachmentsContent(BaseModel):
  model_config = ConfigDict(extra='forbid', strict=True)

  attachment_count: int = Field(ge=0)
  attachments: list[Attachment]


TYPE = FileType(
  name='pdf',
  signatures=(Signature(b'%PDF-', within=1024),),
  views=(
    View(
      name='text',
      about='Exact page text in text-layer order, with hidden and unmapped text notes.',
      content=TextContent,
      paged=True,
    ),
    View(
      name='links',
      about='Link annotations with their real URI targets and non-hidden anchor text.',
      content=LinksContent,
      paged=True,
    ),
    View(
      name='attachments',
      about='Embedded-file names and exact small UTF-8 text attachments.',
      content=AttachmentsContent,
      paged=False,
    ),
  ),
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
