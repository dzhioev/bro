#!/usr/bin/env python
import json

from bro.bros import ChatGPTBro
from bro.bros.chat_gpt_bro import (
  png_to_content,
  image_file_to_content,
  text_to_content,
  ResponseInputParam,
)
from dataclasses import dataclass
from icecream import ic
from pydantic import BaseModel
from json import JSONEncoder
from datetime import datetime
from typing import Type, TypeVar, Any
from abc import abstractmethod, ABC


class Markdown(BaseModel):
  markdown: str


class Content(ABC):
  @abstractmethod
  def dump(self) -> dict[str, Any]: ...


@dataclass
class ImageFile(Content):
  image_path: str

  def dump(self) -> dict[str, Any]:
    return image_file_to_content(self.image_path)


@dataclass
class PngImage(Content):
  image: bytes

  def dump(self) -> dict[str, Any]:
    return png_to_content(self.image)


@dataclass
class Text(Content):
  text: str

  def dump(self) -> dict[str, Any]:
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

  def dump(self) -> dict[str, Any]:
    return text_to_content(json.dumps(self.json, indent=4, cls=self.encoder))


def create_input(prompt: str, *args: Content) -> ResponseInputParam:
  result = []
  result.append({'role': 'system', 'content': prompt})
  content = []
  result.append({'role': 'user', 'content': content})
  for arg in args:
    content.append(arg.dump())
  return result


T = TypeVar('T', bound=BaseModel)


def mu(prompt: str, result: Type[T], *args: Content) -> T:
  client = ChatGPTBro.create().client
  response = client.responses.parse(
    model='gpt-5.1-2025-11-13',
    input=create_input(prompt, *args),
    reasoning={'effort': None},
    text_format=result,
  )
  if response.output_parsed is None:
    response_str = ic.format(response)
    raise RuntimeError(f'no parsed output in response: {response_str}')
  return response.output_parsed
