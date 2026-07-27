"""Observer seam for durable LLM run recording."""

from abc import ABC, abstractmethod
from typing import Any, Literal, Optional

StepKind = Literal[
  'system_prompt',
  'user_input',
  'reasoning',
  'assistant',
  'tool_call',
  'tool_result',
  'llm_call',
  'error',
  'end',
]

EndReason = Literal['ok', 'raised', 'error']


class Tracker(ABC):
  """Capture the LLM event stream in a durable sink."""

  current_tool_step_id: Optional[str] = None

  @abstractmethod
  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    forked_from: Optional[Any],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    """Open a trail and return its id."""
    ...

  @abstractmethod
  def step(self, kind: StepKind, body: Any, **extras: Any) -> Optional[str]:
    """Append one event and return its sink-assigned id when available."""
    ...

  @abstractmethod
  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
    """Close the current trail; repeated calls are ignored."""
    ...


class NullTracker(Tracker):
  """No-op tracker for explicit recording opt-out and tests."""

  def start_trail(
    self,
    bro: str,
    llm_spec: dict,
    system_prompt: str,
    forked_from: Optional[Any],
    interactive: bool,
    surface: str,
    hold: str = 'unattended',
    summoned_by: Optional[dict[str, Any]] = None,
  ) -> str:
    return ''

  def step(self, kind: StepKind, body: Any, **extras: Any) -> Optional[str]:
    return None

  def end_trail(self, reason: EndReason, detail: Optional[str] = None) -> None:
    pass
