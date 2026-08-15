"""Provider-neutral live events and their observer sink."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TurnStartedEvent:
  input: str


@dataclass(frozen=True)
class ReasoningEvent:
  content: str


@dataclass(frozen=True)
class InterimAssistantTextEvent:
  content: str


@dataclass(frozen=True)
class ToolCallEvent:
  call_id: str
  tool_name: str
  arguments: dict[str, Any]

  def __post_init__(self) -> None:
    if len(self.call_id) == 0 or len(self.tool_name) == 0:
      raise ValueError('observed tool call identity and name must not be empty')


@dataclass(frozen=True)
class ToolResultEvent:
  call_id: str
  tool_name: str
  result: Any
  is_error: bool = False

  def __post_init__(self) -> None:
    if len(self.call_id) == 0 or len(self.tool_name) == 0:
      raise ValueError('observed tool result identity and name must not be empty')


@dataclass(frozen=True)
class TurnCompletedEvent:
  content: str


@dataclass(frozen=True)
class TurnRefusedEvent:
  reason: str


@dataclass(frozen=True)
class TurnFailedEvent:
  error: str


type ObservedEvent = (
  TurnStartedEvent
  | ReasoningEvent
  | InterimAssistantTextEvent
  | ToolCallEvent
  | ToolResultEvent
  | TurnCompletedEvent
  | TurnRefusedEvent
  | TurnFailedEvent
)


class Observer(ABC):
  """Receive provider-neutral events from one live run or conversation."""

  @abstractmethod
  def on_event(self, event: ObservedEvent) -> None: ...


class NullObserver(Observer):
  """Explicitly discard observed events."""

  def on_event(self, event: ObservedEvent) -> None:
    pass
