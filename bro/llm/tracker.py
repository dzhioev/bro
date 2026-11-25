"""Observer seam for durable LLM run recording."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Optional, TypedDict

StepKind = Literal[
  'system_prompt', 'user_input', 'notification', 'tool_result', 'llm_call', 'error'
]

EndReason = Literal['ok', 'raised', 'error']


class ToolStepSource(TypedDict):
  step_id: int
  index: int


@dataclass(frozen=True)
class RunFork:
  run: str
  step: int


class Tracker(ABC):
  """Capture the LLM event stream in a durable sink."""

  current_tool_step_id: Optional[ToolStepSource] = None

  @abstractmethod
  def start(
    self,
    bro: str,
    recipe: dict,
    system_prompt: str,
    forked_from: Optional[RunFork],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    session_at: Optional[int] = None,
  ) -> None:
    """Open a run."""
    ...

  @abstractmethod
  def step(self, kind: StepKind, body: Any, **extras: Any) -> Optional[int]:
    """Append one event and return its step number when counted."""
    ...

  @abstractmethod
  def end(self, reason: EndReason, detail: Optional[str] = None) -> None:
    """Close the current run; repeated calls are ignored."""
    ...


class NullTracker(Tracker):
  """No-op tracker for explicit recording opt-out and tests."""

  def start(
    self,
    bro: str,
    recipe: dict,
    system_prompt: str,
    forked_from: Optional[RunFork],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    session_at: Optional[int] = None,
  ) -> None:
    pass

  def step(self, kind: StepKind, body: Any, **extras: Any) -> Optional[int]:
    return None

  def end(self, reason: EndReason, detail: Optional[str] = None) -> None:
    pass
