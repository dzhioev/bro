"""A session's end as the hook that stops its turn and the runner share it:
the service tools that end a session, and the call whose turn the hook stopped."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Self

from bro.monitor import SESSION_DIR_ENV, harness_session_dir

# the bro service tools that end a session, as Claude names them
ANSWER_TOOL = 'mcp__bro__answer'
RAISE_TOOL = 'mcp__bro__raise'

_FILENAME = 'stopped-call'


@dataclass(frozen=True)
class StoppedCall:
  """the call whose turn the hook stopped, and the transcript Claude writes that turn to."""

  call_id: str
  transcript: Path


class StoppedCallMark:
  def __init__(self, path: Path):
    self.path = path

  @classmethod
  def for_session(cls) -> Self:
    state = harness_session_dir('claude')
    if state is None:
      raise RuntimeError(f'{SESSION_DIR_ENV} is unset: a stopped call needs a session state dir')
    return cls(state / _FILENAME)

  def clear(self) -> None:
    """Forget the call an earlier session in this state dir stopped."""
    self.path.unlink(missing_ok=True)

  def record(self, call: StoppedCall) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    staging = self.path.with_name(f'{_FILENAME}.{os.getpid()}.tmp')
    staging.write_text(json.dumps({'call_id': call.call_id, 'transcript': str(call.transcript)}))
    os.replace(staging, self.path)

  def read(self) -> Optional[StoppedCall]:
    try:
      raw = self.path.read_text()
    except FileNotFoundError:
      return None
    recorded = json.loads(raw)
    return StoppedCall(recorded['call_id'], Path(recorded['transcript']))
