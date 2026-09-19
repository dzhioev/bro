"""Broker wire format and JSON codec.

This module owns encoding only.
A `Message` serializes to UTF-8 JSON with no delimiter (`to_bytes`) and parses back from those bytes (`from_bytes`).
Framing belongs to the transport adapter; the TCP adapter uses NDJSON.

The envelope has four types:
a `request` opens a quest (`id`, `payload: {kind, args}`), `mark` carries a lifecycle transition, `message` carries kind-defined chat payload in either direction, and `result` closes the quest.
The type set is closed and the codec enforces every shape, so malformed envelopes fail at the boundary.
The channel attach handshake carries `PROTOCOL_REVISION` for incompatible wire changes.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal, Optional, cast

from bro.base.lulid import lulid

PROTOCOL_REVISION = 4
MAX_FRAME_BYTES = 512 * 1024
MAX_IDENTIFIER_BYTES = 4096
TALK_ENV = 'BROKER_TALK'

OUTCOMES = frozenset({'ok', 'denied', 'failed'})
_MARK_TRANSITIONS = frozenset({'accepted', 'listening', 'started', 'trail'})

type End = Literal['summoner', 'summoned']
type TalkRight = Literal[
  'summoner.say',
  'summoner.question',
  'summoned.say',
  'summoned.question',
]
type Talk = frozenset[TalkRight]
TALK_RIGHTS: Talk = frozenset(
  {'summoner.say', 'summoner.question', 'summoned.say', 'summoned.question'}
)
EMPTY_TALK: Talk = frozenset()

_REQUEST_ENVELOPE_KEYS = frozenset({'type', 'id', 'payload'})
_CORRELATED_ENVELOPE_KEYS = frozenset({'type', 'quest', 'payload'})
_MESSAGE_ENVELOPE_KEYS = _CORRELATED_ENVELOPE_KEYS | {'id', 'reply_to'}
_ENVELOPE_KEYS = _REQUEST_ENVELOPE_KEYS | _MESSAGE_ENVELOPE_KEYS
_RESULT_KEYS = frozenset({'outcome', 'value', 'error', 'detail'})


class ProtocolError(Exception):
  """A wire-level violation: a malformed message, or a frame over `MAX_FRAME_BYTES`."""


class Tag:
  """The four message types; closed — a capability is a kind, not a type."""

  REQUEST = 'request'
  MARK = 'mark'
  MESSAGE = 'message'
  RESULT = 'result'


@dataclass(frozen=True)
class Message:
  type: str
  payload: dict[str, Any]
  id: Optional[str] = None
  quest: Optional[str] = None
  reply_to: Optional[str] = None

  def __post_init__(self):
    _validate(self.type, self.id, self.quest, self.reply_to, self.payload)

  @property
  def quest_id(self) -> str:
    """The quest this envelope belongs to."""
    identifier = self.id if self.type == Tag.REQUEST else self.quest
    assert identifier is not None
    return identifier

  @property
  def kind(self) -> str:
    """The capability a request names (requests only)."""
    if self.type != Tag.REQUEST:
      raise ProtocolError(f'a {self.type} message names no kind')
    return self.payload['kind']

  @property
  def args(self) -> dict[str, Any]:
    """A request's kind arguments (requests only)."""
    if self.type != Tag.REQUEST:
      raise ProtocolError(f'a {self.type} message carries no args')
    return self.payload['args']

  @property
  def outcome(self) -> str:
    """A result's outcome (results only)."""
    if self.type != Tag.RESULT:
      raise ProtocolError(f'a {self.type} message carries no outcome')
    return self.payload['outcome']

  @property
  def is_say(self) -> bool:
    """Whether this is an uncorrelated chat message."""
    self._require_message_role()
    return self.id is None and self.reply_to is None

  @property
  def is_question(self) -> bool:
    """Whether this chat message opens a question, including a counter-question."""
    self._require_message_role()
    return self.id is not None

  @property
  def is_reply(self) -> bool:
    """Whether this chat message answers a question."""
    self._require_message_role()
    return self.reply_to is not None

  def _require_message_role(self) -> None:
    if self.type != Tag.MESSAGE:
      raise ProtocolError(f'a {self.type} message has no chat role')

  def to_bytes(self) -> bytes:
    if self.type == Tag.REQUEST:
      wire: dict[str, Any] = {'type': self.type, 'id': self.id, 'payload': self.payload}
    else:
      wire = {'type': self.type, 'quest': self.quest, 'payload': self.payload}
      if self.type == Tag.MESSAGE:
        if self.id is not None:
          wire['id'] = self.id
        if self.reply_to is not None:
          wire['reply_to'] = self.reply_to
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
    elif message_type in (Tag.MARK, Tag.RESULT):
      _require_envelope_keys(parsed, _CORRELATED_ENVELOPE_KEYS)
    elif message_type == Tag.MESSAGE:
      _require_message_envelope_keys(parsed)
    payload = _require(parsed, 'payload', dict)
    return cls(
      type=message_type,
      payload=payload,
      id=parsed.get('id'),
      quest=parsed.get('quest'),
      reply_to=parsed.get('reply_to'),
    )


def request(kind: str, args: dict[str, Any]) -> Message:
  """A fresh request opening a quest, with its lulid minted here."""
  return Message(type=Tag.REQUEST, payload={'kind': kind, 'args': args}, id=lulid())


def mark(quest_id: str, transition: str, **details: Any) -> Message:
  return Message(
    type=Tag.MARK,
    payload={'transition': transition, **details},
    quest=quest_id,
  )


def message(
  quest_id: str,
  payload: dict[str, Any],
  *,
  id: Optional[str] = None,
  reply_to: Optional[str] = None,
) -> Message:
  return Message(type=Tag.MESSAGE, payload=payload, quest=quest_id, id=id, reply_to=reply_to)


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


def encode_talk(talk: Collection[str]) -> str:
  """Encode quest talk for `BROKER_TALK`: sorted and comma-joined, empty for none."""
  values = set(talk)
  unknown = sorted(values - TALK_RIGHTS)
  if len(unknown) > 0:
    raise ValueError(f'unknown talk right(s): {", ".join(unknown)}')
  return ','.join(sorted(values))


def decode_talk(value: str) -> Talk:
  """Decode and validate one published `BROKER_TALK` value."""
  if not isinstance(value, str):
    raise TypeError('broker talk must be a string')
  values = value.split(',') if len(value) > 0 else []
  if any(len(right) == 0 for right in values):
    raise ValueError('broker talk contains an empty right')
  if len(values) != len(set(values)):
    raise ValueError('broker talk contains a duplicate right')
  unknown = sorted(set(values) - TALK_RIGHTS)
  if len(unknown) > 0:
    raise ValueError(f'unknown talk right(s): {", ".join(unknown)}')
  return cast(Talk, frozenset(values))


def message_allowed(talk: Talk, sender: End, candidate: Message) -> bool:
  """Whether `sender` may send this chat role under a quest's fixed talk."""
  if candidate.type != Tag.MESSAGE:
    raise ProtocolError(f'cannot apply talk to a {candidate.type} message')
  other: End = 'summoned' if sender == 'summoner' else 'summoner'
  if candidate.is_say:
    return f'{sender}.say' in talk
  if candidate.is_reply and f'{other}.question' not in talk:
    return False
  return not candidate.is_question or f'{sender}.question' in talk


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


def _validate(type_: Any, id_: Any, quest_id: Any, reply_to: Any, payload: Any) -> None:
  if not isinstance(payload, dict):
    raise ProtocolError(f"message 'payload' must be dict, got {type(payload).__name__}")
  if type_ == Tag.REQUEST:
    if not isinstance(id_, str) or len(id_) == 0:
      raise ProtocolError("a request needs a non-empty string 'id'")
    _validate_identifier_size('request id', id_)
    if quest_id is not None:
      raise ProtocolError("a request carries no 'quest' field")
    if reply_to is not None:
      raise ProtocolError("a request carries no 'reply_to' field")
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
  if type_ not in (Tag.MARK, Tag.MESSAGE, Tag.RESULT):
    raise ProtocolError(f'unknown message type {type_!r}')
  if not isinstance(quest_id, str) or len(quest_id) == 0:
    raise ProtocolError(f"a {type_} needs a non-empty string 'quest'")
  _validate_identifier_size(f'{type_} quest', quest_id)
  if type_ != Tag.MESSAGE:
    if id_ is not None:
      raise ProtocolError(f"a {type_} carries no 'id' field")
    if reply_to is not None:
      raise ProtocolError(f"a {type_} carries no 'reply_to' field")
  else:
    _validate_optional_identifier('message id', id_)
    _validate_optional_identifier('message reply_to', reply_to)
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


def _validate_optional_identifier(name: str, value: Any) -> None:
  if value is None:
    return
  if not isinstance(value, str) or len(value) == 0:
    raise ProtocolError(f'{name} must be a non-empty string')
  _validate_identifier_size(name, value)


def encoded_text_bytes(value: str) -> int:
  """what `value` costs inside a frame: its JSON string encoding, quotes aside."""
  return len(json.dumps(value, ensure_ascii=False).encode('utf-8')) - 2


def _validate_identifier_size(name: str, value: str) -> None:
  size = encoded_text_bytes(value)
  if size > MAX_IDENTIFIER_BYTES:
    raise ProtocolError(f'{name} encodes to {size} bytes, over {MAX_IDENTIFIER_BYTES}')


def _require_message_envelope_keys(data: dict) -> None:
  missing = sorted(_CORRELATED_ENVELOPE_KEYS - set(data))
  if len(missing) > 0:
    raise ProtocolError(f'message missing required key(s): {", ".join(missing)}')
  forbidden = sorted(set(data) - _MESSAGE_ENVELOPE_KEYS)
  if len(forbidden) > 0:
    raise ProtocolError(f'a message carries no {", ".join(repr(key) for key in forbidden)} field')
  null_identifiers = [key for key in ('id', 'reply_to') if key in data and data[key] is None]
  if len(null_identifiers) > 0:
    raise ProtocolError(
      f'a message omits rather than nulls {", ".join(repr(key) for key in null_identifiers)}'
    )


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
