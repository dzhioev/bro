"""Read a mission's live authority through its own broker channel."""

import json
import os
from functools import lru_cache
from typing import Any

from bro.broker.environment import BROKER_CHANNEL

_INITIAL_AUTHORITY_FILENAME = 'initial-authority.json'


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
  from bro.monitor import session_dir

  directory = session_dir()
  if directory is not None:
    path = directory / _INITIAL_AUTHORITY_FILENAME
    if path.exists():
      snapshot = json.loads(path.read_text())
      if snapshot['channel'] != channel:
        raise ValueError('initial authority snapshot belongs to another broker channel')
      return snapshot['value']
  return read()


def capture_initial() -> None:
  from bro.monitor import session_dir

  directory = session_dir()
  channel = os.environ.get(BROKER_CHANNEL)
  if directory is None or channel is None:
    raise RuntimeError('capturing initial authority requires a brokered session')
  value = read()
  if value is None:
    raise RuntimeError('authority channel disappeared while capturing initial authority')
  (directory / _INITIAL_AUTHORITY_FILENAME).write_text(
    json.dumps({'channel': channel, 'value': value})
  )
  _initial.cache_clear()


def initial() -> dict[str, Any] | None:
  channel = os.environ.get(BROKER_CHANNEL)
  return None if channel is None else _initial(channel)
