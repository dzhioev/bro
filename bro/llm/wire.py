"""The client port for a conversation protocol."""

from __future__ import annotations

import base64
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from bro.llm.records import Call, Chaining, DecodedCall, Part, Stop

if TYPE_CHECKING:
  from pydantic import BaseModel

  from bro.llm.llm import LLMSpec
  from bro.llm.mcp import Tool
  from bro.trails.model import Step


@dataclass(frozen=True)
class SystemPrompt:
  text: str


MIME_TYPE = re.compile(r'[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*')

# OpenAI images and file-inputs guides, read 2026-10-31:
# https://developers.openai.com/api/docs/guides/images-vision
# https://developers.openai.com/api/docs/guides/file-inputs
EXTENSION_TYPES: dict[str, str] = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.pdf': 'application/pdf',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.xla': 'application/vnd.ms-excel',
  '.xlb': 'application/vnd.ms-excel',
  '.xlc': 'application/vnd.ms-excel',
  '.xlm': 'application/vnd.ms-excel',
  '.xls': 'application/vnd.ms-excel',
  '.xlt': 'application/vnd.ms-excel',
  '.xlw': 'application/vnd.ms-excel',
  '.csv': 'text/csv',
  '.tsv': 'text/tsv',
  '.iif': 'text/x-iif',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.doc': 'application/msword',
  '.dot': 'application/msword',
  '.odt': 'application/vnd.oasis.opendocument.text',
  '.rtf': 'application/rtf',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.pot': 'application/vnd.ms-powerpoint',
  '.ppa': 'application/vnd.ms-powerpoint',
  '.pps': 'application/vnd.ms-powerpoint',
  '.ppt': 'application/vnd.ms-powerpoint',
  '.pwz': 'application/vnd.ms-powerpoint',
  '.wiz': 'application/vnd.ms-powerpoint',
  '.bat': 'text/plain',
  '.conf': 'text/plain',
  '.def': 'text/plain',
  '.dic': 'text/plain',
  '.in': 'text/plain',
  '.list': 'text/plain',
  '.log': 'text/plain',
  '.text': 'text/plain',
  '.txt': 'text/plain',
  '.markdown': 'text/markdown',
  '.md': 'text/markdown',
  '.htm': 'text/html',
  '.html': 'text/html',
  '.xml': 'text/xml',
  '.json': 'application/json',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.asm': 'text/x-asm',
  '.s': 'text/x-asm',
  '.c': 'text/x-c',
  '.h': 'text/x-c',
  '.cc': 'text/x-c++',
  '.cpp': 'text/x-c++',
  '.cxx': 'text/x-c++',
  '.hh': 'text/x-c++',
  '.pl': 'text/x-perl',
  '.py': 'text/x-python',
  '.ksh': 'text/x-sh',
  '.sql': 'text/x-sql',
  '.rst': 'text/x-rst',
  '.eml': 'message/rfc822',
  '.mht': 'message/rfc822',
  '.mhtml': 'message/rfc822',
  '.mime': 'message/rfc822',
  '.nws': 'message/rfc822',
  '.ics': 'text/calendar',
  '.ifb': 'text/calendar',
  '.vcf': 'text/x-vcard',
  '.srt': 'application/x-subrip',
  '.vtt': 'text/vtt',
}


@dataclass(frozen=True)
class Text:
  text: str

  def __post_init__(self):
    if not isinstance(self.text, str):
      raise TypeError('Text.text must be a string')


@dataclass(frozen=True)
class File:
  data: bytes
  mime_type: str
  filename: str | None = None

  def __post_init__(self):
    if not isinstance(self.data, bytes):
      raise TypeError('File.data must be bytes')
    if not self.data:
      raise ValueError('File.data must not be empty')
    if not isinstance(self.mime_type, str):
      raise TypeError('File.mime_type must be a string')
    if MIME_TYPE.fullmatch(self.mime_type) is None:
      raise ValueError('File.mime_type must be a lowercase MIME type without parameters')
    if self.filename is not None:
      if not isinstance(self.filename, str):
        raise TypeError('File.filename must be a string or None')
      if not self.filename:
        raise ValueError('File.filename must not be empty')

  @classmethod
  def from_path(cls, path: str | os.PathLike[str], mime_type: str | None = None) -> File:
    from bro.files import access

    filename = Path(path).name
    if mime_type is None:
      mime_type = EXTENSION_TYPES.get(Path(filename).suffix.lower())
      if mime_type is None:
        raise ValueError(f'unknown file type for {str(path)!r}; pass mime_type explicitly')
    with access.current().open_read(Path(path)) as stream:
      return cls(stream.read(), mime_type, filename)

  def data_url(self) -> str:
    return f'data:{self.mime_type};base64,{base64.b64encode(self.data).decode("ascii")}'


Content = Text | File


@dataclass(frozen=True)
class UserMessage:
  content: tuple[Content, ...]

  def __post_init__(self):
    if not isinstance(self.content, tuple):
      raise TypeError('UserMessage.content must be a tuple')
    if not self.content:
      raise ValueError('UserMessage.content must not be empty')
    if any(not isinstance(item, (Text, File)) for item in self.content):
      raise TypeError('UserMessage.content items must be Text or File')


def require_accepted(recipe: LLMSpec, content: tuple[Content, ...]) -> None:
  from bro.llm.llm import UnsupportedContent

  for item in content:
    if isinstance(item, File) and not recipe.accepts(item.mime_type):
      raise UnsupportedContent(
        f'{item.mime_type} content is not supported by {recipe.selection()["model"]}'
      )


@dataclass(frozen=True)
class Notification:
  text: str


@dataclass(frozen=True)
class ToolOutput:
  call_id: str
  output: dict | str


Input = SystemPrompt | UserMessage | Notification | ToolOutput


@dataclass(frozen=True)
class Prefix:
  system_prompt: str
  steps: tuple[Step, ...]


@dataclass(frozen=True)
class Leg:
  record: dict
  extras: dict
  wire: str
  model: str
  counts: dict[str, int] | None
  parts: tuple[Part, ...]
  stop: Stop
  refusal: str | None
  reply: str | None
  stop_reason: str | None = None

  @property
  def calls(self) -> tuple[Call, ...]:
    return tuple(part for part in self.parts if isinstance(part, Call))

  @classmethod
  def from_decoded(
    cls,
    record: dict,
    extras: dict,
    wire: str,
    decoded: DecodedCall,
    counts: dict[str, int] | None,
  ) -> Leg:
    return cls(
      record,
      extras,
      wire,
      decoded.model,
      counts,
      decoded.parts,
      decoded.stop,
      decoded.refusal,
      decoded.reply,
      decoded.stop_reason,
    )


class UnusableResponse(RuntimeError):
  """A received leg whose terminal reply cannot be extracted.

  accepted reports whether the wire advanced its conversation state.
  """

  def __init__(self, message: str, leg: Leg, *, accepted: bool):
    super().__init__(message)
    self.leg = leg
    self.accepted = accepted


class Wire(ABC):
  NAME: ClassVar[str]
  CHAINING: ClassVar[Chaining]
  OWNED: ClassVar[frozenset[str]]

  @abstractmethod
  def __init__(self, recipe: LLMSpec, secrets: dict): ...

  @abstractmethod
  async def exchange(
    self, inputs: list[Input], tools: list[Tool], request_timeout: float | None
  ) -> Leg: ...

  @abstractmethod
  def restore(self, prefix: Prefix) -> None: ...

  def seed_chain(self, response_id: str) -> None:
    raise ValueError(f'wire {self.NAME!r} does not support server chains')

  async def structured[T: BaseModel](
    self,
    instructions: str,
    result: type[T],
    content: tuple[Content, ...],
    request_timeout: float | None,
  ) -> T:
    raise NotImplementedError(f'wire {self.NAME!r} has no structured one-shot')
