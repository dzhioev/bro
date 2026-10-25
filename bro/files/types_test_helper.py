from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from bro.files.types import FileType, Limits, Signature, View


class Details(BaseModel):
  model_config = ConfigDict(extra='forbid')

  title: str
  page_count: int

  @field_validator('title')
  @classmethod
  def normalize_title(cls, value: str) -> str:
    return value.strip()


class Page(BaseModel):
  page_number: int
  text: str


class Content(BaseModel):
  pages: list[Page]


class OtherContent(BaseModel):
  count: int


def make_type(directory: Path, **changes: Any) -> FileType:
  manual = directory / 'manual.md'
  manual.write_text('title page_count pages page_number text count', encoding='utf-8')
  return replace(
    FileType(
      name='sample',
      signatures=(Signature(b'SAMPLE', within=2),),
      views=(
        View('text', 'Read page text', Content, True),
        View('summary', 'Read the count', OtherContent, False),
      ),
      details=Details,
      provider='missing_parser.provider:build',
      limits=Limits(max_bytes=1000, max_pages=5000, max_view_characters=100, max_view_entries=20),
      manual=manual,
    ),
    **changes,
  )


class EntryPoint:
  def __init__(self, name: str, value: object):
    self.name = name
    self.value = f'test:{name}'
    self._value = value
    self.loaded = 0

  def load(self):
    self.loaded += 1
    if isinstance(self._value, Exception):
      raise self._value
    return self._value
