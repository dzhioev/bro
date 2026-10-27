import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from json import JSONEncoder
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bro.base import credentials
from bro.llm import providers, wires
from bro.llm.wire import Wire


class Markdown(BaseModel):
  markdown: str


class Content(ABC):
  @abstractmethod
  def to_text(self) -> str: ...


@dataclass
class Text(Content):
  text: str

  def to_text(self) -> str:
    return self.text


class DateTimeEncoder(json.JSONEncoder):
  def default(self, o):
    if isinstance(o, datetime):
      return o.isoformat()
    return super().default(o)


@dataclass
class JSON(Content):
  json: dict[str, Any]
  encoder: type[JSONEncoder] | None = None

  def to_text(self) -> str:
    return json.dumps(self.json, indent=4, cls=self.encoder)


class _Pair(BaseModel):
  model_config = ConfigDict(strict=True, extra='forbid', hide_input_in_errors=True)

  llm: str = Field(min_length=1)
  credential: dict[str, Any] = Field(min_length=1)


class _Material(_Pair):
  roles: dict[str, _Pair] = Field(default_factory=dict)


def _configured_wire(role: str | None) -> Wire:
  try:
    material = _Material.model_validate(credentials.get_json('mu'))
    pair = material.roles.get(role, material) if role is not None else material
    selection = providers.parse(pair.llm)
    if selection.provider is None:
      raise ValueError('llm must name a provider')
    recipe = providers.resolve(
      providers.default_spec(selection.provider), selection, providers.known_names()
    )
    wire = wires.open(recipe, pair.credential)
    if type(wire).structured is Wire.structured:
      raise ValueError(f'wire {wire.NAME!r} has no structured one-shot')
    return wire
  except Exception as error:
    raise ValueError(f'mu credential: {error}') from error


class _Mu:
  def __call__[T: BaseModel](
    self, prompt: str, result: type[T], *contents: Content, role: str | None = None
  ) -> T:
    return asyncio.run(self.aio(prompt, result, *contents, role=role))

  async def aio[T: BaseModel](
    self, prompt: str, result: type[T], *contents: Content, role: str | None = None
  ) -> T:
    wire = _configured_wire(role)
    parts = [{'type': 'text', 'text': content.to_text()} for content in contents]
    return await wire.structured(prompt, result, parts, request_timeout=None)


mu = _Mu()
