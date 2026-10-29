"""SDK-free meanings of recorded wire responses."""

import json
from dataclasses import dataclass
from typing import Any, Literal

Stop = Literal['completed', 'truncated', 'filtered', 'capacity', 'failed']
Chaining = Literal['server', 'client']


@dataclass(frozen=True)
class Reasoning:
  index: int
  text: str


@dataclass(frozen=True)
class Message:
  index: int
  text: str
  commentary: bool = False


@dataclass(frozen=True)
class Call:
  index: int
  call_id: str | None
  name: str | None
  raw_arguments: str
  arguments: Any
  arguments_are_object: bool

  @classmethod
  def parse(cls, index: int, call_id: Any, name: Any, raw_arguments: Any) -> 'Call':
    if isinstance(raw_arguments, str):
      try:
        arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        arguments_are_object = isinstance(arguments, dict)
      except json.JSONDecodeError:
        arguments = {'_raw_arguments': raw_arguments}
        arguments_are_object = False
      raw = raw_arguments
    else:
      arguments = raw_arguments
      arguments_are_object = isinstance(arguments, dict)
      raw = json.dumps(raw_arguments, ensure_ascii=False)
    return cls(
      index,
      call_id if isinstance(call_id, str) else None,
      name if isinstance(name, str) else None,
      raw,
      arguments,
      arguments_are_object,
    )


@dataclass(frozen=True)
class Unknown:
  index: int
  raw: Any


Part = Reasoning | Message | Call | Unknown


@dataclass(frozen=True)
class DecodedCall:
  model: str
  usage: dict
  service_tier: str | None
  parts: tuple[Part, ...]
  stop: Stop
  refusal: str | None = None
  stop_reason: str | None = None

  @property
  def calls(self) -> tuple[Call, ...]:
    return tuple(part for part in self.parts if isinstance(part, Call))

  @property
  def reply(self) -> str | None:
    if self.calls:
      return None
    texts = [part.text for part in self.parts if isinstance(part, Message) and not part.commentary]
    return '\n\n'.join(texts)

  @property
  def terminal(self) -> bool:
    return self.stop == 'completed' and not self.calls
