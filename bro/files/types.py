"""Import-cheap declarations for file-type plug-ins."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from bro.files.text import validate_strings

IDENTIFICATION_BYTES = 4096
_NAME = re.compile(r'[a-z][a-z0-9-]*')
_PROVIDER = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*')


def type_name(value: str) -> str:
  if not isinstance(value, str) or _NAME.fullmatch(value) is None:
    raise ValueError(f'invalid file-type name {value!r}; expected [a-z][a-z0-9-]*')
  return value


def _positive(name: str, value: int) -> None:
  if type(value) is not int or value <= 0:
    raise ValueError(f'{name} must be a positive integer')


def _model(value: type[BaseModel]) -> None:
  if not isinstance(value, type) or not issubclass(value, BaseModel):
    raise TypeError('file-type details and content must be pydantic model classes')


@dataclass(frozen=True)
class Signature:
  marker: bytes
  within: int

  def __post_init__(self) -> None:
    if not isinstance(self.marker, bytes) or not self.marker:
      raise ValueError('a signature marker must be nonempty bytes')
    if type(self.within) is not int or self.within < 0:
      raise ValueError('signature within must be a nonnegative integer')
    if self.within + len(self.marker) > IDENTIFICATION_BYTES:
      raise ValueError(f'a signature must fit within the first {IDENTIFICATION_BYTES} bytes')

  def matches(self, head: bytes) -> bool:
    return head.find(self.marker, 0, self.within + len(self.marker)) >= 0


@dataclass(frozen=True)
class View:
  name: str
  about: str
  content: type[BaseModel]
  paged: bool

  def __post_init__(self) -> None:
    type_name(self.name)
    if not self.about.strip() or self.about.splitlines() != [self.about]:
      raise ValueError('view about must be one nonempty line')
    validate_strings(self.about)
    _model(self.content)
    if type(self.paged) is not bool:
      raise TypeError('view paged must be a boolean')


@dataclass(frozen=True)
class Limits:
  max_bytes: int
  max_pages: Optional[int]
  max_view_characters: int
  max_view_entries: int

  def __post_init__(self) -> None:
    for name in ('max_bytes', 'max_view_characters', 'max_view_entries'):
      _positive(name, getattr(self, name))
    if self.max_pages is not None:
      _positive('max_pages', self.max_pages)


@dataclass(frozen=True)
class FileType:
  name: str
  signatures: tuple[Signature, ...]
  views: tuple[View, ...]
  details: type[BaseModel]
  provider: str
  limits: Limits
  manual: Path

  def __post_init__(self) -> None:
    type_name(self.name)
    if not isinstance(self.signatures, tuple) or not all(
      isinstance(signature, Signature) for signature in self.signatures
    ):
      raise TypeError('file-type signatures must be a tuple of Signature instances')
    if not isinstance(self.views, tuple) or not all(isinstance(view, View) for view in self.views):
      raise TypeError('file-type views must be a tuple of View instances')
    if len({view.name for view in self.views}) != len(self.views):
      raise ValueError(f'duplicate view in file type {self.name!r}')
    _model(self.details)
    if _PROVIDER.fullmatch(self.provider) is None:
      raise ValueError('file-type provider must be module:attribute')
    if not isinstance(self.limits, Limits):
      raise TypeError('file-type limits must be a Limits instance')
    if self.limits.max_pages is None and any(view.paged for view in self.views):
      raise ValueError('a file type without pages cannot declare a paged view')
    if not isinstance(self.manual, Path):
      raise TypeError('file-type manual must be a Path')

  def get_view(self, name: str) -> View:
    for view in self.views:
      if view.name == name:
        return view
    installed = ', '.join(view.name for view in self.views) or '(none)'
    raise ValueError(f'unknown view {name!r} for file type {self.name!r}; use one of: {installed}')
