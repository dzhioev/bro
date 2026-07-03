"""broker wire format — the typed, versioned message and its JSON codec.

This module owns *encoding* only. A `Message` serializes to UTF-8 JSON with no
delimiter (`to_bytes`) and parses back from those bytes (`from_bytes`). *Framing* —
how messages are delimited on a byte stream — is the transport adapter's concern
(the unix adapter uses NDJSON, `json + '\\n'`). Keeping that seam here means a
future websocket adapter reuses this exact encoding under native frames.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ulid import ULID

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1 << 20  # 1 MiB; result is model-bounded text, so generous but capped


class ProtocolError(Exception):
  """a wire-level violation: malformed message JSON, or a frame over MAX_FRAME_BYTES."""


class Tag:
  """substrate built-in message-type tags; consumer tags are open strings."""

  STARTED = 'started'  # {trail_id}
  COMPLETED = 'completed'  # {result, end_reason}  end_reason: terminal|raised|error
  FAILED = 'failed'  # {reason: 'exit'|'timeout', exit_code?, output_tail?}
  REPLY = 'reply'  # generic correlated reply (in_reply_to set); the type context.reply emits
  PING = 'ping'  # acceptance round-trip
  SPAWN = 'spawn'  # acceptance: spawn a throwaway child


def _new_id() -> str:
  return str(ULID())


@dataclass(frozen=True)
class Message:
  type: str  # message-type tag; consumers add tags, not transports
  payload: dict[str, Any]
  id: str = field(default_factory=_new_id)  # this message's id (ULID), minted unless supplied
  in_reply_to: Optional[str] = None  # request id this replies to; None for a fresh send
  v: int = PROTOCOL_VERSION

  def to_bytes(self) -> bytes:
    return json.dumps(
      {
        'type': self.type,
        'id': self.id,
        'in_reply_to': self.in_reply_to,
        'payload': self.payload,
        'v': self.v,
      },
      ensure_ascii=False,
    ).encode('utf-8')

  @classmethod
  def from_bytes(cls, raw: bytes) -> 'Message':
    try:
      parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
      raise ProtocolError(f'malformed message JSON: {e}') from e
    if not isinstance(parsed, dict):
      raise ProtocolError(f'message must be a JSON object, got {type(parsed).__name__}')
    type_ = _require(parsed, 'type', str)
    id_ = _require(parsed, 'id', str)
    payload = _require(parsed, 'payload', dict)
    in_reply_to = parsed.get('in_reply_to')
    if in_reply_to is not None and not isinstance(in_reply_to, str):
      raise ProtocolError(
        f"message 'in_reply_to' must be a string or null, got {type(in_reply_to).__name__}"
      )
    v = parsed.get('v', PROTOCOL_VERSION)
    if not isinstance(v, int):
      raise ProtocolError(f"message 'v' must be an int, got {type(v).__name__}")
    return cls(type=type_, payload=payload, id=id_, in_reply_to=in_reply_to, v=v)


def _require(data: dict, key: str, kind: type) -> Any:
  if key not in data:
    raise ProtocolError(f'message missing required key {key!r}')
  value = data[key]
  if not isinstance(value, kind):
    raise ProtocolError(f'message {key!r} must be {kind.__name__}, got {type(value).__name__}')
  return value
