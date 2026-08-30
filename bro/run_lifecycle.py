"""Worker-process lifecycle emission over its broker channel."""

import os
from typing import TYPE_CHECKING, Any, Optional

from bro.llm.tracker import EndReason

if TYPE_CHECKING:
  from bro.broker.client import Client

MAX_ANSWER_BYTES = 64 << 10
ANSWER_TOO_LARGE = 'answer too large; mint an artifact and return the ref'


class RunLifecycle:
  def __init__(self, client: 'Client', quest: str):
    self._client = client
    self._quest = quest
    self._trail_id: Optional[str] = None

  @classmethod
  def from_env(cls) -> Optional['RunLifecycle']:
    try:
      from bro.broker.client import QUEST_ENV, Client
    except ImportError:
      return None
    client = Client.from_env()
    if client is None:
      return None
    quest = os.environ.get(QUEST_ENV)
    if quest is None:
      client.close()
      raise ValueError(
        f'broker channel present but {QUEST_ENV} unset; '
        'the launch did not name the quest this run answers'
      )
    return cls(client, quest)

  def trail(self, trail_id: str) -> None:
    if self._trail_id is not None:
      raise RuntimeError('run lifecycle trail already emitted')
    self._trail_id = trail_id
    self._client.mark(self._quest, 'trail', trail_id=trail_id)

  def completed(
    self,
    result: Optional[str],
    end_reason: EndReason,
    *,
    trail_id: Optional[str] = None,
  ) -> None:
    payload: dict[str, Any]
    effective_trail = self._trail_id if self._trail_id is not None else trail_id
    bounded = truncate_answer(result, effective_trail) if result is not None else None
    if end_reason == 'ok':
      payload = {'outcome': 'ok'}
      if bounded is not None:
        payload['value'] = bounded
    else:
      payload = {'outcome': 'failed', 'detail': {'reason': end_reason}}
      if bounded is not None:
        payload['error'] = bounded
    self._client.result(self._quest, payload)

  def close(self) -> None:
    self._client.close(confirm=True)


def validate_answer(answer: str) -> None:
  if len(answer.encode()) > MAX_ANSWER_BYTES:
    raise ValueError(ANSWER_TOO_LARGE)


def truncate_answer(answer: str, trail_id: Optional[str]) -> str:
  encoded = answer.encode()
  if len(encoded) <= MAX_ANSWER_BYTES:
    return answer
  location = trail_id[:256] if trail_id is not None else 'unavailable'
  marker = f'\n\n[answer truncated; full response in trail {location}]'
  marker_bytes = marker.encode()
  head = encoded[: MAX_ANSWER_BYTES - len(marker_bytes)].decode(errors='ignore')
  return head + marker
