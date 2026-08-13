#!/usr/bin/env python
import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from json import JSONEncoder
from typing import Any, Optional

from icecream import ic
from openai import pydantic_function_tool
from openai.types.responses import Response, ResponseInputParam, ResponseTextConfigParam
from openai.types.responses.response_input_content_param import ResponseInputContentParam
from openai.types.shared.reasoning_effort import ReasoningEffort
from pydantic import BaseModel

from bro.base import credentials
from bro.llm.llms.chat_gpt import (
  image_file_to_content,
  image_to_content,
  pdf_to_content,
  png_to_content,
  text_to_content,
)

DEFAULT_MODEL = 'gpt-5.6-terra'


class IncompleteResponse(Exception):
  """The model stopped before it finished the reply."""


class TruncatedResponse(IncompleteResponse):
  """The model ran out of output tokens before it finished the reply."""


class Markdown(BaseModel):
  markdown: str


class Content(ABC):
  @abstractmethod
  def dump(self) -> ResponseInputContentParam: ...


@dataclass
class ImageFile(Content):
  image_path: str

  def dump(self) -> ResponseInputContentParam:
    return image_file_to_content(self.image_path)


@dataclass
class PngImage(Content):
  image: bytes

  def dump(self) -> ResponseInputContentParam:
    return png_to_content(self.image)


@dataclass
class Image(Content):
  data: bytes
  mime_type: str

  def dump(self) -> ResponseInputContentParam:
    return image_to_content(self.data, self.mime_type)


@dataclass
class PDFFile(Content):
  data: bytes
  filename: str

  def dump(self) -> ResponseInputContentParam:
    return pdf_to_content(self.data, self.filename)


@dataclass
class Text(Content):
  text: str

  def dump(self) -> ResponseInputContentParam:
    return text_to_content(self.text)


class DateTimeEncoder(json.JSONEncoder):
  def default(self, o):
    if isinstance(o, datetime):
      return o.isoformat()
    return super().default(o)


@dataclass
class JSON(Content):
  json: dict[str, Any]
  encoder: Optional[type[JSONEncoder]] = None

  def dump(self) -> ResponseInputContentParam:
    return text_to_content(json.dumps(self.json, indent=4, cls=self.encoder))


@dataclass
class JSONList(Content):
  items: list[BaseModel]

  def dump(self) -> ResponseInputContentParam:
    return text_to_content(json.dumps([item.model_dump() for item in self.items], indent=2))


def create_input(prompt: str, *args: Content) -> ResponseInputParam:
  result = []
  result.append({'role': 'system', 'content': prompt})
  content = []
  result.append({'role': 'user', 'content': content})
  for arg in args:
    content.append(arg.dump())
  return result


def _text_format(result: type[BaseModel]) -> ResponseTextConfigParam:
  # the SDK derives an OpenAI strict schema only inside its function-tool helper; the
  # Responses text format needs that same schema under its own keys
  function = pydantic_function_tool(result)['function']
  schema = function.get('parameters')
  assert schema is not None
  return {
    'format': {'type': 'json_schema', 'name': function['name'], 'schema': schema, 'strict': True}
  }


def _completed_text(response: Response) -> str:
  """The response's output text, rejecting a reply the model never finished."""
  if response.status == 'incomplete':
    details = response.incomplete_details
    reason = details.reason if details is not None else None
    usage = response.usage
    tokens = (
      f'{usage.input_tokens} input / {usage.output_tokens} output tokens'
      if usage is not None
      else 'usage not reported'
    )
    if reason == 'max_output_tokens':
      raise TruncatedResponse(f'response truncated at the output-token limit ({tokens})')
    raise IncompleteResponse(f'response incomplete, reason {reason} ({tokens})')
  if len(response.output_text) == 0:
    response_str = ic.format(response)
    raise RuntimeError(f'no output text in response: {response_str}')
  return response.output_text


class _Mu:
  def __call__[T: BaseModel](
    self,
    prompt: str,
    result: type[T],
    *args: Content,
    model: str = DEFAULT_MODEL,
    reasoning_effort: ReasoningEffort = None,
  ) -> T:
    return asyncio.run(
      self.aio(prompt, result, *args, model=model, reasoning_effort=reasoning_effort)
    )

  async def aio[T: BaseModel](
    self,
    prompt: str,
    result: type[T],
    *args: Content,
    model: str = DEFAULT_MODEL,
    reasoning_effort: ReasoningEffort = None,
  ) -> T:
    from openai import AsyncOpenAI

    # responses.parse would validate the partial text of a truncated reply and surface the
    # cut-off as a schema error, so the completion check has to run on an unparsed response
    async with AsyncOpenAI(api_key=credentials.get_json('openai')['api_key']) as client:
      response = await client.responses.create(
        model=model,
        input=create_input(prompt, *args),
        reasoning={'effort': reasoning_effort},
        text=_text_format(result),
      )
    return result.model_validate_json(_completed_text(response))


mu = _Mu()
