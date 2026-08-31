"""broker wire format — the four-type message and its JSON codec.

This module owns *encoding* only.
A `Message` serializes to UTF-8 JSON with no delimiter (`to_bytes`) and parses back from those bytes (`from_bytes`).
*Framing* — how messages are delimited on a byte stream — is the transport adapter's concern (the tcp adapter uses NDJSON, `json + '\\n'`).

The envelope has four types and nothing else:
a `request` opens a quest (`id`, `payload: {kind, args}`), and `mark` / `progress` / `result` describe one (`quest` names it).
A mark carries a lifecycle transition, progress carries kind-defined interim data, and a result carries `outcome` plus optional `value` / `error` / `detail`.
The type set is closed — a new capability is a new kind, never a new type — and the codec enforces the whole shape, so a malformed envelope fails at the boundary rather than deep in routing.
There is no version field:
a version belongs to the channel, and the host provisions every channel and launches every peer, so both ends are the same release.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional

from bro.base.lulid import lulid

PROTOCOL_REVISION = 2
MAX_FRAME_BYTES = 256 * 1024
MAX_IDENTIFIER_BYTES = 4096

OUTCOMES = frozenset({'ok', 'denied', 'failed'})
_MARK_TRANSITIONS = frozenset({'accepted', 'started', 'trail'})

_REQUEST_ENVELOPE_KEYS = frozenset({'type', 'id', 'payload'})
_CORRELATED_ENVELOPE_KEYS = frozenset({'type', 'quest', 'payload'})
_ENVELOPE_KEYS = _REQUEST_ENVELOPE_KEYS | _CORRELATED_ENVELOPE_KEYS
_RESULT_KEYS = frozenset({'outcome', 'value', 'error', 'detail'})


class ProtocolError(Exception):
  """a wire-level violation: a malformed message, or a frame over MAX_FRAME_BYTES."""


class Tag:
  """the four message types; closed — a capability is a kind, not a type."""

  REQUEST = 'request'
  MARK = 'mark'
  PROGRESS = 'progress'
  RESULT = 'result'


@dataclass(frozen=True)
class Message:
  type: str
  payload: dict[str, Any]
  id: Optional[str] = None
  quest: Optional[str] = None

  def __post_init__(self):
    _validate(self.type, self.id, self.quest, self.payload)

  @property
  def quest_id(self) -> str:
    """the quest this message belongs to."""
    identifier = self.id if self.type == Tag.REQUEST else self.quest
    assert identifier is not None
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
      wire = {'type': self.type, 'quest': self.quest, 'payload': self.payload}
    return json.dumps(wire, ensure_ascii=False).encode('utf-8')

  @classmethod
  def from_bytes(cls, raw: bytes) -> 'Message':
    try:
      parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
      raise ProtocolError(f'malformed message JSON: {error}') from error
    if not isinstance(parsed, dict):
      raise ProtocolError(f'message must be a JSON object, got {type(parsed).__name__}')
    unknown = sorted(set(parsed) - _ENVELOPE_KEYS)
    if len(unknown) > 0:
      raise ProtocolError(f'unknown message key(s): {", ".join(unknown)}')
    message_type = _require(parsed, 'type', str)
    if message_type == Tag.REQUEST:
      _require_envelope_keys(parsed, _REQUEST_ENVELOPE_KEYS)
    elif message_type in (Tag.MARK, Tag.PROGRESS, Tag.RESULT):
      _require_envelope_keys(parsed, _CORRELATED_ENVELOPE_KEYS)
    payload = _require(parsed, 'payload', dict)
    return cls(type=message_type, payload=payload, id=parsed.get('id'), quest=parsed.get('quest'))


def request(kind: str, args: dict[str, Any]) -> Message:
  """a fresh request opening a quest, its id minted here (a lulid)."""
  return Message(type=Tag.REQUEST, payload={'kind': kind, 'args': args}, id=lulid())


def mark(quest_id: str, transition: str, **details: Any) -> Message:
  return Message(
    type=Tag.MARK,
    payload={'transition': transition, **details},
    quest=quest_id,
  )


def progress(quest_id: str, payload: dict[str, Any]) -> Message:
  return Message(type=Tag.PROGRESS, payload=payload, quest=quest_id)


def result(
  quest_id: str,
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
  return Message(type=Tag.RESULT, payload=payload, quest=quest_id)


def frame_safe_result(message: Message) -> Message:
  if message.type != Tag.RESULT:
    raise ProtocolError(f'cannot fit a {message.type} message as a result')
  if len(message.to_bytes()) <= MAX_FRAME_BYTES:
    return message
  original_error = message.payload.get('error')
  error = (
    original_error
    if isinstance(original_error, str)
    else 'result payload exceeded the broker frame bound'
  )
  outcome = message.outcome if message.outcome != 'ok' else 'failed'
  detail = {'truncated': True}
  lower = 0
  upper = len(error)
  while lower < upper:
    middle = (lower + upper + 1) // 2
    candidate = result(message.quest_id, outcome, error=error[:middle], detail=detail)
    if len(candidate.to_bytes()) <= MAX_FRAME_BYTES:
      lower = middle
    else:
      upper = middle - 1
  fitted = result(message.quest_id, outcome, error=error[:lower], detail=detail)
  if len(fitted.to_bytes()) > MAX_FRAME_BYTES:
    raise ProtocolError('a result identifier leaves no room for a bounded payload')
  return fitted


def _validate(type_: Any, id_: Any, quest_id: Any, payload: Any) -> None:
  if not isinstance(payload, dict):
    raise ProtocolError(f"message 'payload' must be dict, got {type(payload).__name__}")
  if type_ == Tag.REQUEST:
    if not isinstance(id_, str) or len(id_) == 0:
      raise ProtocolError("a request needs a non-empty string 'id'")
    _validate_identifier_size('request id', id_)
    if quest_id is not None:
      raise ProtocolError("a request carries no 'quest' field")
    kind = payload.get('kind')
    if not isinstance(kind, str) or len(kind) == 0:
      raise ProtocolError("a request payload needs a non-empty string 'kind'")
    _validate_identifier_size('request kind', kind)
    if not isinstance(payload.get('args'), dict):
      raise ProtocolError("a request payload needs dict 'args'")
    unknown = sorted(set(payload) - {'kind', 'args'})
    if len(unknown) > 0:
      raise ProtocolError(f'unknown request payload key(s): {", ".join(unknown)}')
    return
  if type_ not in (Tag.MARK, Tag.PROGRESS, Tag.RESULT):
    raise ProtocolError(f'unknown message type {type_!r}')
  if not isinstance(quest_id, str) or len(quest_id) == 0:
    raise ProtocolError(f"a {type_} needs a non-empty string 'quest'")
  _validate_identifier_size(f'{type_} quest', quest_id)
  if id_ is not None:
    raise ProtocolError(f"a {type_} carries no 'id' field")
  if type_ == Tag.MARK:
    transition = payload.get('transition')
    if not isinstance(transition, str) or transition not in _MARK_TRANSITIONS:
      raise ProtocolError(
        f"a mark payload needs 'transition' of {', '.join(sorted(_MARK_TRANSITIONS))}"
      )
    if transition == 'trail':
      trail_id = payload.get('trail_id')
      if not isinstance(trail_id, str) or len(trail_id) == 0:
        raise ProtocolError("a trail mark needs a non-empty string 'trail_id'")
      _validate_identifier_size('trail id', trail_id)
  if type_ == Tag.RESULT:
    if payload.get('outcome') not in OUTCOMES:
      raise ProtocolError(f"a result payload needs 'outcome' of {', '.join(sorted(OUTCOMES))}")
    unknown = sorted(set(payload) - _RESULT_KEYS)
    if len(unknown) > 0:
      raise ProtocolError(f'unknown result payload key(s): {", ".join(unknown)}')
    error = payload.get('error')
    if error is not None and not isinstance(error, str):
      raise ProtocolError("a result payload's 'error' must be a string")
    detail = payload.get('detail')
    if detail is not None and not isinstance(detail, dict):
      raise ProtocolError("a result payload's 'detail' must be an object")
    reason = detail.get('reason') if isinstance(detail, dict) else None
    if reason is not None and not isinstance(reason, str):
      raise ProtocolError("a result detail's 'reason' must be a string")


def _validate_identifier_size(name: str, value: str) -> None:
  size = len(value.encode('utf-8'))
  if size > MAX_IDENTIFIER_BYTES:
    raise ProtocolError(f'{name} is {size} bytes, over {MAX_IDENTIFIER_BYTES}')


def _require_envelope_keys(data: dict, expected: frozenset[str]) -> None:
  missing = sorted(expected - set(data))
  if len(missing) > 0:
    raise ProtocolError(f'message missing required key(s): {", ".join(missing)}')
  forbidden = sorted(set(data) - expected)
  if len(forbidden) > 0:
    raise ProtocolError(
      f'a {data["type"]} carries no {", ".join(repr(key) for key in forbidden)} field'
    )


def _require(data: dict, key: str, kind: type) -> Any:
  if key not in data:
    raise ProtocolError(f'message missing required key {key!r}')
  value = data[key]
  if not isinstance(value, kind):
    raise ProtocolError(f'message {key!r} must be {kind.__name__}, got {type(value).__name__}')
  return value
