"""Read a mission's live authority through its own broker channel."""

import os
from functools import lru_cache
from typing import Any

from bro.broker.environment import BROKER_CHANNEL


def read() -> dict[str, Any] | None:
  if not os.environ.get(BROKER_CHANNEL):
    return None
  from bro.broker.client import Client

  client = Client.from_env()
  if client is None:
    raise RuntimeError('authority channel disappeared')
  with client:
    result = client.call('authority', {}, timeout=30).payload
  if result.get('outcome') != 'ok':
    raise ValueError(f'authority request refused: {result}')
  return result['value']


@lru_cache
def _initial(channel: str) -> dict[str, Any] | None:
  return read()


def initial() -> dict[str, Any] | None:
  channel = os.environ.get(BROKER_CHANNEL)
  return None if channel is None else _initial(channel)
