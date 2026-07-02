import json

import pytest

from broker.brotocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, Message, ProtocolError


def test_round_trip():
  message = Message(type='ping', payload={'who': 'a', 'n': 1}, in_reply_to='req-1')
  back = Message.from_bytes(message.to_bytes())
  assert back == message


def test_id_auto_minted_and_unique():
  a = Message(type='ping', payload={})
  b = Message(type='ping', payload={})
  assert len(a.id) == 26  # ULID
  assert a.id != b.id


def test_defaults():
  message = Message(type='ping', payload={})
  assert message.in_reply_to is None
  assert message.v == PROTOCOL_VERSION


def test_v_field_round_trips():
  raw = json.dumps({'type': 'x', 'id': 'i', 'in_reply_to': None, 'payload': {}, 'v': 7}).encode(
    'utf-8'
  )
  assert Message.from_bytes(raw).v == 7


def test_v_defaults_when_absent():
  raw = json.dumps({'type': 'x', 'id': 'i', 'in_reply_to': None, 'payload': {}}).encode('utf-8')
  assert Message.from_bytes(raw).v == PROTOCOL_VERSION


def test_to_bytes_has_no_framing():
  assert b'\n' not in Message(type='ping', payload={'k': 'v'}).to_bytes()


def test_to_bytes_utf8_round_trip():
  message = Message(type='ping', payload={'msg': 'naïve — café ☕'})
  assert Message.from_bytes(message.to_bytes()) == message


@pytest.mark.parametrize(
  'raw',
  [
    b'not json at all',
    b'[1, 2, 3]',  # JSON, but not an object
    b'"a string"',
    json.dumps({'id': 'i', 'payload': {}}).encode('utf-8'),  # missing 'type'
    json.dumps({'type': 'x', 'id': 'i'}).encode('utf-8'),  # missing 'payload'
    json.dumps({'type': 5, 'id': 'i', 'payload': {}}).encode('utf-8'),  # wrong 'type' kind
    json.dumps({'type': 'x', 'id': 'i', 'payload': []}).encode('utf-8'),  # wrong 'payload' kind
    json.dumps({'type': 'x', 'id': 'i', 'payload': {}, 'in_reply_to': 5}).encode(
      'utf-8'
    ),  # bad in_reply_to
  ],
)
def test_from_bytes_malformed_raises(raw):
  with pytest.raises(ProtocolError):
    Message.from_bytes(raw)


def test_unknown_keys_ignored():
  raw = json.dumps(
    {'type': 'x', 'id': 'i', 'in_reply_to': None, 'payload': {}, 'v': 1, 'future': 'field'}
  ).encode('utf-8')
  assert Message.from_bytes(raw).type == 'x'


def test_max_frame_bytes_value():
  assert MAX_FRAME_BYTES == 1 << 20
