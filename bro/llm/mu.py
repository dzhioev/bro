import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bro.base import credentials
from bro.llm import providers, wires
from bro.llm.wire import Content, UserMessage, Wire


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
    self, instructions: str, result: type[T], *content: Content, role: str | None = None
  ) -> T:
    return asyncio.run(self.aio(instructions, result, *content, role=role))

  async def aio[T: BaseModel](
    self, instructions: str, result: type[T], *content: Content, role: str | None = None
  ) -> T:
    message = UserMessage(content)
    wire = _configured_wire(role)
    return await wire.structured(instructions, result, message.content, request_timeout=None)


mu = _Mu()
