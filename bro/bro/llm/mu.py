#!/usr/bin/env python
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from json import JSONEncoder
from typing import Any, Optional

from icecream import ic
from openai.types.responses import ResponseInputParam
from openai.types.shared.reasoning_effort import ReasoningEffort
from pydantic import BaseModel

from llm.llms import ChatGPT
from llm.llms.chat_gpt import (
  ResponseInputContentPart,
  image_file_to_content,
  image_to_content,
  pdf_to_content,
  png_to_content,
  text_to_content,
)


class Markdown(BaseModel):
  markdown: str


class Content(ABC):
  @abstractmethod
  def dump(self) -> ResponseInputContentPart: ...


@dataclass
class ImageFile(Content):
  image_path: str

  def dump(self) -> ResponseInputContentPart:
    return image_file_to_content(self.image_path)


@dataclass
class PngImage(Content):
  image: bytes

  def dump(self) -> ResponseInputContentPart:
    return png_to_content(self.image)


@dataclass
class Image(Content):
  data: bytes
  mime_type: str

  def dump(self) -> ResponseInputContentPart:
    return image_to_content(self.data, self.mime_type)


@dataclass
class PDFFile(Content):
  data: bytes
  filename: str

  def dump(self) -> ResponseInputContentPart:
    return pdf_to_content(self.data, self.filename)


@dataclass
class Text(Content):
  text: str

  def dump(self) -> ResponseInputContentPart:
    return text_to_content(self.text)


class DateTimeEncoder(json.JSONEncoder):
  def default(self, o):
    if isinstance(o, datetime):
      return o.isoformat()
    return super().default(o)


@dataclass
class Json(Content):
  json: dict[str, Any]
  encoder: Optional[type[JSONEncoder]] = None

  def dump(self) -> ResponseInputContentPart:
    return text_to_content(json.dumps(self.json, indent=4, cls=self.encoder))


@dataclass
class JsonList(Content):
  items: list[BaseModel]

  def dump(self) -> ResponseInputContentPart:
    return text_to_content(json.dumps([item.model_dump() for item in self.items], indent=2))


def create_input(prompt: str, *args: Content) -> ResponseInputParam:
  result = []
  result.append({'role': 'system', 'content': prompt})
  content = []
  result.append({'role': 'user', 'content': content})
  for arg in args:
    content.append(arg.dump())
  return result


def mu[T: BaseModel](
  prompt: str, result: type[T], *args: Content, reasoning_effort: ReasoningEffort = None
) -> T:
  client = ChatGPT.create().client
  response = client.responses.parse(
    model='gpt-5.1-2025-11-13',
    input=create_input(prompt, *args),
    reasoning={'effort': reasoning_effort},
    text_format=result,
  )
  if response.output_parsed is None:
    response_str = ic.format(response)
    raise RuntimeError(f'no parsed output in response: {response_str}')
  return response.output_parsed
