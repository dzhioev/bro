import json

import pytest

from bro.broker.brotocol import (
  MAX_FRAME_BYTES,
  MAX_IDENTIFIER_BYTES,
  Message,
  ProtocolError,
  Tag,
  frame_safe_result,
  mark,
  progress,
  request,
  result,
)


def test_request_round_trip():
  message = request('ping', {'who': 'a', 'n': 1})
  assert Message.from_bytes(message.to_bytes()) == message


def test_mark_round_trip():
  message = mark('quest-1', 'trail', trail_id='trail-1')
  assert Message.from_bytes(message.to_bytes()) == message


def test_progress_round_trip():
  message = progress('quest-1', {'note': 'working'})
  assert Message.from_bytes(message.to_bytes()) == message


def test_result_round_trip():
  message = result('quest-1', 'failed', value='v', error='boom', detail={'reason': 'exit'})
  back = Message.from_bytes(message.to_bytes())
  assert back == message
  assert back.payload == {'outcome': 'failed', 'value': 'v', 'error': 'boom', 'detail': {'reason': 'exit'}}  # fmt: skip


def test_frame_safe_result_bounds_an_oversize_error_with_a_visible_marker():
  message = result('quest-1', 'denied', error='x' * MAX_FRAME_BYTES)

  fitted = frame_safe_result(message)

  assert len(fitted.to_bytes()) <= MAX_FRAME_BYTES
  assert fitted.outcome == 'denied'
  assert fitted.payload['detail']['truncated'] is True
  assert fitted.payload['error'] == message.payload['error'][: len(fitted.payload['error'])]


def test_request_id_minted_and_unique():
  first = request('ping', {})
  second = request('ping', {})
  assert len(first.quest_id) == 28
  assert first.id != second.id


def test_quest_id_unifies_opening_and_correlated_messages():
  opened = request('ping', {})
  assert opened.quest_id == opened.id
  assert mark('quest-1', 'accepted').quest_id == 'quest-1'
  assert progress('quest-1', {}).quest_id == 'quest-1'
  assert result('quest-1', 'ok').quest_id == 'quest-1'


def test_request_wire_carries_no_quest_key():
  wire = json.loads(request('ping', {}).to_bytes())
  assert set(wire) == {'type', 'id', 'payload'}


def test_correlated_wire_carries_no_id_key():
  wire = json.loads(result('quest-1', 'ok').to_bytes())
  assert set(wire) == {'type', 'quest', 'payload'}


def test_accessors():
  opened = request('summon', {'target': 'bro'})
  assert opened.kind == 'summon'
  assert opened.args == {'target': 'bro'}
  assert result('quest-1', 'ok', value='').outcome == 'ok'


def test_accessors_reject_the_wrong_type():
  with pytest.raises(ProtocolError):
    _ = result('quest-1', 'ok').kind
  with pytest.raises(ProtocolError):
    _ = progress('quest-1', {}).args
  with pytest.raises(ProtocolError):
    _ = request('ping', {}).outcome


def test_unknown_mark_transition_is_rejected():
  with pytest.raises(ProtocolError, match='accepted, started, trail'):
    mark('quest-1', 'finished')


def test_oversize_identifiers_are_rejected():
  oversize = 'x' * (MAX_IDENTIFIER_BYTES + 1)
  with pytest.raises(ProtocolError, match='request id'):
    Message(type=Tag.REQUEST, id=oversize, payload={'kind': 'ping', 'args': {}})
  with pytest.raises(ProtocolError, match='request kind'):
    Message(type=Tag.REQUEST, id='request', payload={'kind': oversize, 'args': {}})
  with pytest.raises(ProtocolError, match='progress quest'):
    progress(oversize, {})
  with pytest.raises(ProtocolError, match='trail id'):
    mark('quest', 'trail', trail_id=oversize)


def test_wire_frame_cap_is_256_kibibytes():
  assert MAX_FRAME_BYTES == 256 * 1024


def test_to_bytes_has_no_framing():
  assert b'\n' not in request('ping', {'k': 'v'}).to_bytes()


def test_to_bytes_utf8_round_trip():
  message = progress('quest-1', {'msg': 'naïve — café ☕'})
  assert Message.from_bytes(message.to_bytes()) == message


@pytest.mark.parametrize(
  'kwargs',
  [
    {'type': 'started', 'payload': {}, 'id': 'i'},
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}}},
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}}, 'id': 'i', 'quest': 'q'},
    {'type': 'request', 'payload': {'args': {}}, 'id': 'i'},
    {'type': 'request', 'payload': {'kind': 'ping'}, 'id': 'i'},
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}, 'extra': 1}, 'id': 'i'},
    {'type': 'mark', 'payload': {'transition': 'accepted'}},
    {'type': 'mark', 'payload': {}, 'quest': 'q'},
    {'type': 'mark', 'payload': {'transition': 'unknown'}, 'quest': 'q'},
    {'type': 'mark', 'payload': {'transition': 'trail'}, 'quest': 'q'},
    {'type': 'progress', 'payload': {}},
    {'type': 'progress', 'payload': {}, 'quest': 'q', 'id': 'i'},
    {'type': 'result', 'payload': {}, 'quest': 'q'},
    {'type': 'result', 'payload': {'outcome': 'done'}, 'quest': 'q'},
    {'type': 'result', 'payload': {'outcome': 'ok', 'extra': 1}, 'quest': 'q'},
    {'type': 'result', 'payload': {'outcome': 'failed', 'error': 7}, 'quest': 'q'},
    {'type': 'result', 'payload': {'outcome': 'failed', 'detail': []}, 'quest': 'q'},
    {'type': 'result', 'payload': {'outcome': 'failed', 'detail': {'reason': 7}}, 'quest': 'q'},
    {'type': 'result', 'payload': [], 'quest': 'q'},
  ],
)
def test_construction_rejects_malformed_envelopes(kwargs):
  with pytest.raises(ProtocolError):
    Message(**kwargs)


@pytest.mark.parametrize(
  'wire',
  [
    {'type': 'request', 'id': 'i', 'quest': None, 'payload': {'kind': 'ping', 'args': {}}},
    {'type': 'mark', 'id': None, 'quest': 'q', 'payload': {'transition': 'accepted'}},
    {'type': 'progress', 'id': None, 'quest': 'q', 'payload': {}},
    {'type': 'result', 'id': None, 'quest': 'q', 'payload': {'outcome': 'ok'}},
  ],
)
def test_from_bytes_rejects_forbidden_null_envelope_fields(wire):
  with pytest.raises(ProtocolError):
    Message.from_bytes(json.dumps(wire).encode('utf-8'))


@pytest.mark.parametrize(
  'raw',
  [
    b'not json at all',
    b'[1, 2, 3]',
    b'"a string"',
    json.dumps({'id': 'i', 'payload': {}}).encode('utf-8'),
    json.dumps({'type': 'request', 'id': 'i'}).encode('utf-8'),
    json.dumps({'type': 5, 'id': 'i', 'payload': {}}).encode('utf-8'),
    json.dumps({'type': 'request', 'id': 'i', 'payload': []}).encode('utf-8'),
    json.dumps({'type': 'result', 'quest': 5, 'payload': {'outcome': 'ok'}}).encode('utf-8'),
    json.dumps(
      {'type': 'request', 'id': 'i', 'payload': {'kind': 'ping', 'args': {}}, 'v': 1}
    ).encode('utf-8'),
  ],
)
def test_from_bytes_malformed_raises(raw):
  with pytest.raises(ProtocolError):
    Message.from_bytes(raw)
