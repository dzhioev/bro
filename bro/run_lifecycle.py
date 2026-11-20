"""Worker-process lifecycle emission over its broker channel."""

import os
from typing import TYPE_CHECKING, Any, Optional

from bro.llm.tracker import EndReason
from bro.monitor import trail_pointer

if TYPE_CHECKING:
  from bro.broker.client import Client

MAX_ANSWER_BYTES = 64 << 10
UNANSWERED = 'unanswered'


class RunLifecycle:
  def __init__(self, client: 'Client', mission: str):
    self._client = client
    self._mission = mission
    self._trail_id: Optional[str] = None

  @classmethod
  def from_env(cls) -> Optional['RunLifecycle']:
    try:
      from bro.broker.client import Client
      from bro.broker.environment import BROKER_MISSION
    except ImportError:
      return None
    client = Client.from_env()
    if client is None:
      return None
    mission = os.environ.get(BROKER_MISSION)
    if mission is None:
      client.close()
      raise ValueError(
        f'broker channel present but {BROKER_MISSION} unset; '
        'the launch did not name the mission this run undertakes'
      )
    return cls(client, mission)

  def trail(self, trail_id: str) -> None:
    if self._trail_id is not None:
      raise RuntimeError('run lifecycle trail already emitted')
    self._trail_id = trail_id
    self._client.mark(self._mission, 'trail', trail_id=trail_id)

  def completed(
    self,
    result: Optional[str],
    end_reason: EndReason,
  ) -> None:
    payload: dict[str, Any]
    pointer = trail_pointer.path()
    trail_id = trail_pointer.read(pointer) if pointer is not None else None
    bounded = truncate_answer(result, trail_id) if result is not None else None
    if end_reason == 'ok':
      payload = {'outcome': 'ok'}
      if bounded is not None:
        payload['value'] = bounded
    else:
      payload = {'outcome': 'failed', 'detail': {'reason': end_reason}}
      if bounded is not None:
        payload['error'] = bounded
    self._client.result(self._mission, payload)

  def unanswered(self) -> None:
    """Fail the mission of a run that ended without delivering its answer."""
    self._client.result(
      self._mission,
      {
        'outcome': 'failed',
        'detail': {'reason': UNANSWERED},
        'error': 'the run ended without calling `answer`',
      },
    )

  def close(self) -> None:
    self._client.close(confirm=True)


def validate_answer(answer: str) -> None:
  size = len(answer.encode())
  if size > MAX_ANSWER_BYTES:
    raise ValueError(
      f'answer too large: {size} bytes, over the {MAX_ANSWER_BYTES}-byte answer bound; '
      'shorten it to fit'
    )


def truncate_answer(answer: str, trail_id: Optional[str]) -> str:
  encoded = answer.encode()
  if len(encoded) <= MAX_ANSWER_BYTES:
    return answer
  location = trail_id[:256] if trail_id is not None else 'unavailable'
  marker = f'\n\n[answer truncated; full response in trail {location}]'
  marker_bytes = marker.encode()
  head = encoded[: MAX_ANSWER_BYTES - len(marker_bytes)].decode(errors='ignore')
  return head + marker
