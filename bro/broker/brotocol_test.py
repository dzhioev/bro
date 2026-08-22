import json

import pytest

from bro.broker.brotocol import (
  MAX_FRAME_BYTES,
  Message,
  ProtocolError,
  progress,
  request,
  result,
)


def test_request_round_trip():
  message = request('ping', {'who': 'a', 'n': 1})
  back = Message.from_bytes(message.to_bytes())
  assert back == message


def test_progress_round_trip():
  message = progress('req-1', {'trail_id': 't-1'})
  assert Message.from_bytes(message.to_bytes()) == message


def test_result_round_trip():
  message = result('req-1', 'failed', value='v', error='boom', detail={'reason': 'exit'})
  back = Message.from_bytes(message.to_bytes())
  assert back == message
  assert back.payload == {'outcome': 'failed', 'value': 'v', 'error': 'boom', 'detail': {'reason': 'exit'}}  # fmt: skip


def test_request_id_minted_and_unique():
  a = request('ping', {})
  b = request('ping', {})
  assert len(a.exchange) == 28  # lulid: 26 ULID chars + 2 dashes
  assert a.id != b.id


def test_request_wire_carries_no_request_key():
  wire = json.loads(request('ping', {}).to_bytes())
  assert set(wire) == {'type', 'id', 'payload'}


def test_answer_wire_carries_no_id_key():
  wire = json.loads(result('req-1', 'ok').to_bytes())
  assert set(wire) == {'type', 'request', 'payload'}


def test_accessors():
  opened = request('summon', {'target': 'bro'})
  assert opened.kind == 'summon'
  assert opened.args == {'target': 'bro'}
  assert result('req-1', 'ok', value='').outcome == 'ok'


def test_accessors_reject_the_wrong_type():
  with pytest.raises(ProtocolError):
    _ = result('req-1', 'ok').kind
  with pytest.raises(ProtocolError):
    _ = progress('req-1', {}).args
  with pytest.raises(ProtocolError):
    _ = request('ping', {}).outcome


def test_to_bytes_has_no_framing():
  assert b'\n' not in request('ping', {'k': 'v'}).to_bytes()


def test_to_bytes_utf8_round_trip():
  message = progress('req-1', {'msg': 'naïve — café ☕'})
  assert Message.from_bytes(message.to_bytes()) == message


@pytest.mark.parametrize(
  'kwargs',
  [
    {'type': 'started', 'payload': {}, 'id': 'i'},  # a type outside the three
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}}},  # no id
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}}, 'id': 'i', 'request': 'r'},
    {'type': 'request', 'payload': {'args': {}}, 'id': 'i'},  # no kind
    {'type': 'request', 'payload': {'kind': 'ping'}, 'id': 'i'},  # no args
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}, 'extra': 1}, 'id': 'i'},
    {'type': 'progress', 'payload': {}},  # no request
    {'type': 'progress', 'payload': {}, 'request': 'r', 'id': 'i'},  # an id on an answer
    {'type': 'result', 'payload': {}, 'request': 'r'},  # no outcome
    {'type': 'result', 'payload': {'outcome': 'done'}, 'request': 'r'},  # outcome off the enum
    {'type': 'result', 'payload': {'outcome': 'ok', 'extra': 1}, 'request': 'r'},
    {'type': 'result', 'payload': [], 'request': 'r'},  # wrong payload kind
  ],
)
def test_construction_rejects_malformed_envelopes(kwargs):
  with pytest.raises(ProtocolError):
    Message(**kwargs)


@pytest.mark.parametrize(
  'raw',
  [
    b'not json at all',
    b'[1, 2, 3]',  # JSON, but not an object
    b'"a string"',
    json.dumps({'id': 'i', 'payload': {}}).encode('utf-8'),  # missing 'type'
    json.dumps({'type': 'request', 'id': 'i'}).encode('utf-8'),  # missing 'payload'
    json.dumps({'type': 5, 'id': 'i', 'payload': {}}).encode('utf-8'),  # wrong 'type' kind
    json.dumps({'type': 'request', 'id': 'i', 'payload': []}).encode('utf-8'),
    json.dumps({'type': 'result', 'request': 5, 'payload': {'outcome': 'ok'}}).encode('utf-8'),
    json.dumps(
      {'type': 'request', 'id': 'i', 'payload': {'kind': 'ping', 'args': {}}, 'v': 1}
    ).encode('utf-8'),  # fmt: skip
  ],
)
def test_from_bytes_malformed_raises(raw):
  with pytest.raises(ProtocolError):
    Message.from_bytes(raw)


def test_max_frame_bytes_value():
  assert MAX_FRAME_BYTES == 1 << 20
