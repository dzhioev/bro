#!/usr/bin/env python
import json

from llm.llms import ChatGPT
from llm.llms.chat_gpt import (
  ResponseInputContentPart,
  png_to_content,
  image_file_to_content,
  text_to_content,
)
from openai.types.responses import ResponseInputParam
from openai.types.shared.reasoning_effort import ReasoningEffort
from dataclasses import dataclass
from icecream import ic
from pydantic import BaseModel
from json import JSONEncoder
from datetime import datetime
from typing import Any, Type, TypeVar
from abc import abstractmethod, ABC


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
  encoder: Type[JSONEncoder] | None = None

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


T = TypeVar('T', bound=BaseModel)


def mu(prompt: str, result: Type[T], *args: Content, reasoning_effort: ReasoningEffort = None) -> T:
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
