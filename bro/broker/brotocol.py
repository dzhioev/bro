"""broker wire format — the three-type message and its JSON codec.

This module owns *encoding* only. A `Message` serializes to UTF-8 JSON with no
delimiter (`to_bytes`) and parses back from those bytes (`from_bytes`). *Framing* —
how messages are delimited on a byte stream — is the transport adapter's concern
(the unix adapter uses NDJSON, `json + '\\n'`). Keeping that seam here means a
future websocket adapter reuses this exact encoding under native frames.

The envelope has three types and nothing else: a `request` opens an exchange
(`id`, `payload: {kind, args}`), and `progress` / `result` answer one (`request`
names the exchange; a result's payload carries `outcome` plus optional `value` /
`error` / `detail`). The type set is closed — a new capability is a new kind,
never a new type — and the codec enforces the whole shape, so a malformed
envelope fails at the boundary rather than deep in routing. There is no version
field: a version belongs to the channel, and the host provisions every channel
and launches every peer, so both ends are the same release.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional

from bro.base.lulid import lulid

MAX_FRAME_BYTES = 1 << 20  # 1 MiB; result is model-bounded text, so generous but capped

# a result's outcome: carried out / refused before any work began (deterministic,
# so retrying is pointless) / work began and produced no answer (a retry may succeed)
OUTCOMES = frozenset({'ok', 'denied', 'failed'})

_ENVELOPE_KEYS = frozenset({'type', 'id', 'request', 'payload'})
_RESULT_KEYS = frozenset({'outcome', 'value', 'error', 'detail'})


class ProtocolError(Exception):
  """a wire-level violation: a malformed message, or a frame over MAX_FRAME_BYTES."""


class Tag:
  """the three message types; closed — a capability is a kind, not a type."""

  REQUEST = 'request'  # opens an exchange; payload {kind, args}
  PROGRESS = 'progress'  # zero or more per exchange, informational, ordered
  RESULT = 'result'  # payload {outcome, value?, error?, detail?}; exactly one closes the exchange


@dataclass(frozen=True)
class Message:
  type: str
  payload: dict[str, Any]
  id: Optional[str] = None  # requests only: the exchange this request opens
  request: Optional[str] = None  # progress and result only: the exchange this message belongs to

  def __post_init__(self):
    _validate(self.type, self.id, self.request, self.payload)

  @property
  def exchange(self) -> str:
    """the exchange this message belongs to: the one a request opens (`id`), or
    the one an answer names (`request`)."""
    identifier = self.id if self.type == Tag.REQUEST else self.request
    assert identifier is not None  # presence per type is validated at construction
    return identifier

  @property
  def kind(self) -> str:
    """the capability a request names (requests only)."""
    if self.type != Tag.REQUEST:
      raise ProtocolError(f'a {self.type} message names no kind')
    return self.payload['kind']

  @property
  def args(self) -> dict[str, Any]:
    """a request's kind arguments (requests only)."""
    if self.type != Tag.REQUEST:
      raise ProtocolError(f'a {self.type} message carries no args')
    return self.payload['args']

  @property
  def outcome(self) -> str:
    """a result's outcome (results only)."""
    if self.type != Tag.RESULT:
      raise ProtocolError(f'a {self.type} message carries no outcome')
    return self.payload['outcome']

  def to_bytes(self) -> bytes:
    if self.type == Tag.REQUEST:
      wire: dict[str, Any] = {'type': self.type, 'id': self.id, 'payload': self.payload}
    else:
      wire = {'type': self.type, 'request': self.request, 'payload': self.payload}
    return json.dumps(wire, ensure_ascii=False).encode('utf-8')

  @classmethod
  def from_bytes(cls, raw: bytes) -> 'Message':
    try:
      parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
      raise ProtocolError(f'malformed message JSON: {e}') from e
    if not isinstance(parsed, dict):
      raise ProtocolError(f'message must be a JSON object, got {type(parsed).__name__}')
    unknown = sorted(set(parsed) - _ENVELOPE_KEYS)
    if len(unknown) > 0:
      raise ProtocolError(f'unknown message key(s): {", ".join(unknown)}')
    type_ = _require(parsed, 'type', str)
    payload = _require(parsed, 'payload', dict)
    return cls(type=type_, payload=payload, id=parsed.get('id'), request=parsed.get('request'))


def request(kind: str, args: dict[str, Any]) -> Message:
  """a fresh request opening an exchange, its id minted here (a lulid)."""
  return Message(type=Tag.REQUEST, payload={'kind': kind, 'args': args}, id=lulid())


def progress(request_id: str, payload: dict[str, Any]) -> Message:
  return Message(type=Tag.PROGRESS, payload=payload, request=request_id)


def result(
  request_id: str,
  outcome: str,
  *,
  value: Any = None,
  error: Optional[str] = None,
  detail: Optional[dict[str, Any]] = None,
) -> Message:
  payload: dict[str, Any] = {'outcome': outcome}
  if value is not None:
    payload['value'] = value
  if error is not None:
    payload['error'] = error
  if detail is not None:
    payload['detail'] = detail
  return Message(type=Tag.RESULT, payload=payload, request=request_id)


def _validate(type_: Any, id_: Any, request_id: Any, payload: Any) -> None:
  if not isinstance(payload, dict):
    raise ProtocolError(f"message 'payload' must be dict, got {type(payload).__name__}")
  if type_ == Tag.REQUEST:
    if not isinstance(id_, str) or len(id_) == 0:
      raise ProtocolError("a request needs a non-empty string 'id'")
    if request_id is not None:
      raise ProtocolError("a request carries no 'request' field")
    kind = payload.get('kind')
    if not isinstance(kind, str) or len(kind) == 0:
      raise ProtocolError("a request payload needs a non-empty string 'kind'")
    if not isinstance(payload.get('args'), dict):
      raise ProtocolError("a request payload needs dict 'args'")
    unknown = sorted(set(payload) - {'kind', 'args'})
    if len(unknown) > 0:
      raise ProtocolError(f'unknown request payload key(s): {", ".join(unknown)}')
    return
  if type_ not in (Tag.PROGRESS, Tag.RESULT):
    raise ProtocolError(f'unknown message type {type_!r}')
  if not isinstance(request_id, str) or len(request_id) == 0:
    raise ProtocolError(f"a {type_} needs a non-empty string 'request'")
  if id_ is not None:
    raise ProtocolError(f"a {type_} carries no 'id' field")
  if type_ == Tag.RESULT:
    if payload.get('outcome') not in OUTCOMES:
      raise ProtocolError(f"a result payload needs 'outcome' of {', '.join(sorted(OUTCOMES))}")
    unknown = sorted(set(payload) - _RESULT_KEYS)
    if len(unknown) > 0:
      raise ProtocolError(f'unknown result payload key(s): {", ".join(unknown)}')


def _require(data: dict, key: str, kind: type) -> Any:
  if key not in data:
    raise ProtocolError(f'message missing required key {key!r}')
  value = data[key]
  if not isinstance(value, kind):
    raise ProtocolError(f'message {key!r} must be {kind.__name__}, got {type(value).__name__}')
  return value
