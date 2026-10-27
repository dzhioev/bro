"""The client port for a conversation protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


@dataclass(frozen=True)
class UserMessage:
  content: str | list[dict]


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
  """A received leg whose terminal reply cannot be extracted."""

  def __init__(self, message: str, leg: Leg):
    super().__init__(message)
    self.leg = leg


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
    self, prompt: str, result: type[T], contents: list, request_timeout: float | None
  ) -> T:
    raise NotImplementedError(f'wire {self.NAME!r} has no structured one-shot')
