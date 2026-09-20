import json

import pytest

from bro.broker.brotocol import (
  MAX_FRAME_BYTES,
  MAX_IDENTIFIER_BYTES,
  PROTOCOL_REVISION,
  Message,
  ProtocolError,
  Tag,
  Talk,
  decode_talk,
  encode_talk,
  frame_safe_result,
  mark,
  message,
  message_allowed,
  request,
  result,
)


def test_request_round_trip():
  message = request('ping', {'who': 'a', 'n': 1})
  assert Message.from_bytes(message.to_bytes()) == message


def test_mark_round_trip():
  message = mark('quest-1', 'trail', trail_id='trail-1')
  assert Message.from_bytes(message.to_bytes()) == message


def test_message_round_trip():
  candidate = message('quest-1', {'note': 'working'}, id='question-1', reply_to='question-0')
  assert Message.from_bytes(candidate.to_bytes()) == candidate


def test_message_roles_cover_say_question_reply_and_counter_question():
  say = message('quest', {})
  question = message('quest', {}, id='question')
  reply = message('quest', {}, reply_to='question')
  counter_question = message('quest', {}, id='counter', reply_to='question')

  assert say.is_say and not say.is_question and not say.is_reply
  assert question.is_question and not question.is_say and not question.is_reply
  assert reply.is_reply and not reply.is_say and not reply.is_question
  assert counter_question.is_question and counter_question.is_reply and not counter_question.is_say


def test_talk_encoding_is_canonical_and_strict():
  talk = decode_talk('owner.question,worker.say')
  assert encode_talk(talk) == 'owner.question,worker.say'
  assert decode_talk('') == frozenset()
  with pytest.raises(ValueError, match='unknown talk right'):
    decode_talk('worker.command')
  with pytest.raises(ValueError, match='duplicate'):
    decode_talk('worker.say,worker.say')


def test_talk_roles_require_the_senders_move_and_the_other_ends_question_for_a_reply():
  talk: Talk = frozenset({'owner.say', 'owner.question', 'worker.question'})
  assert message_allowed(talk, 'owner', message('quest', {}))
  assert message_allowed(talk, 'owner', message('quest', {}, id='question'))
  assert message_allowed(talk, 'owner', message('quest', {}, reply_to='worker-question'))
  assert not message_allowed(talk, 'worker', message('quest', {}))
  assert message_allowed(talk, 'worker', message('quest', {}, reply_to='owner-question'))
  assert not message_allowed(
    frozenset({'worker.question'}),
    'worker',
    message('quest', {}, reply_to='owner-question'),
  )
  assert not message_allowed(
    frozenset({'worker.question'}),
    'owner',
    message('quest', {}, id='counter', reply_to='worker-question'),
  )


def test_protocol_revision_identifies_the_message_wire():
  assert PROTOCOL_REVISION == 5


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
  assert len(first.request_id) == 28
  assert first.id != second.id


def test_quest_id_unifies_opening_and_correlated_messages():
  opened = request('ping', {})
  assert opened.request_id == opened.id
  assert mark('quest-1', 'accepted').request_id == 'quest-1'
  assert message('quest-1', {}).request_id == 'quest-1'
  assert result('quest-1', 'ok').request_id == 'quest-1'


def test_request_wire_carries_no_quest_key():
  wire = json.loads(request('ping', {}).to_bytes())
  assert set(wire) == {'type', 'id', 'payload'}


def test_correlated_wire_carries_no_id_key():
  wire = json.loads(result('quest-1', 'ok').to_bytes())
  assert set(wire) == {'type', 'request', 'payload'}


def test_accessors():
  opened = request('summon', {'target': 'bro'})
  assert opened.kind == 'summon'
  assert opened.args == {'target': 'bro'}
  assert result('quest-1', 'ok', value='').outcome == 'ok'


def test_accessors_reject_the_wrong_type():
  with pytest.raises(ProtocolError):
    _ = result('quest-1', 'ok').kind
  with pytest.raises(ProtocolError):
    _ = message('quest-1', {}).args
  with pytest.raises(ProtocolError):
    _ = request('ping', {}).outcome


def test_unknown_mark_transition_is_rejected():
  with pytest.raises(ProtocolError, match='accepted, listening, started, trail'):
    mark('quest-1', 'finished')


def test_oversize_identifiers_are_rejected():
  oversize = 'x' * (MAX_IDENTIFIER_BYTES + 1)
  with pytest.raises(ProtocolError, match='request id'):
    Message(type=Tag.REQUEST, id=oversize, payload={'kind': 'ping', 'args': {}})
  with pytest.raises(ProtocolError, match='request kind'):
    Message(type=Tag.REQUEST, id='request', payload={'kind': oversize, 'args': {}})
  with pytest.raises(ProtocolError, match='message request'):
    message(oversize, {})
  with pytest.raises(ProtocolError, match='trail id'):
    mark('quest', 'trail', trail_id=oversize)


def test_identifier_bound_measures_the_encoded_cost():
  assert message('quest', {}, id='x' * MAX_IDENTIFIER_BYTES).id is not None
  expanding = '\x00' * (MAX_IDENTIFIER_BYTES // 6 + 1)
  assert len(expanding.encode()) < MAX_IDENTIFIER_BYTES
  with pytest.raises(ProtocolError, match='encodes to'):
    message('quest', {}, id=expanding)


def test_wire_frame_cap_is_512_kibibytes():
  assert MAX_FRAME_BYTES == 512 * 1024


def test_to_bytes_has_no_framing():
  assert b'\n' not in request('ping', {'k': 'v'}).to_bytes()


def test_to_bytes_utf8_round_trip():
  candidate = message('quest-1', {'msg': 'naïve — café ☕'})
  assert Message.from_bytes(candidate.to_bytes()) == candidate


@pytest.mark.parametrize(
  'kwargs',
  [
    {'type': 'started', 'payload': {}, 'id': 'i'},
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}}},
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}}, 'id': 'i', 'request': 'q'},
    {'type': 'request', 'payload': {'args': {}}, 'id': 'i'},
    {'type': 'request', 'payload': {'kind': 'ping'}, 'id': 'i'},
    {'type': 'request', 'payload': {'kind': 'ping', 'args': {}, 'extra': 1}, 'id': 'i'},
    {'type': 'mark', 'payload': {'transition': 'accepted'}},
    {'type': 'mark', 'payload': {}, 'request': 'q'},
    {'type': 'mark', 'payload': {'transition': 'unknown'}, 'request': 'q'},
    {'type': 'mark', 'payload': {'transition': 'trail'}, 'request': 'q'},
    {'type': 'message', 'payload': {}},
    {'type': 'message', 'payload': {}, 'request': 'q', 'id': ''},
    {'type': 'result', 'payload': {}, 'request': 'q'},
    {'type': 'result', 'payload': {'outcome': 'done'}, 'request': 'q'},
    {'type': 'result', 'payload': {'outcome': 'ok', 'extra': 1}, 'request': 'q'},
    {'type': 'result', 'payload': {'outcome': 'failed', 'error': 7}, 'request': 'q'},
    {'type': 'result', 'payload': {'outcome': 'failed', 'detail': []}, 'request': 'q'},
    {'type': 'result', 'payload': {'outcome': 'failed', 'detail': {'reason': 7}}, 'request': 'q'},
    {'type': 'result', 'payload': [], 'request': 'q'},
  ],
)
def test_construction_rejects_malformed_envelopes(kwargs):
  with pytest.raises(ProtocolError):
    Message(**kwargs)


@pytest.mark.parametrize(
  'wire',
  [
    {'type': 'request', 'id': 'i', 'request': None, 'payload': {'kind': 'ping', 'args': {}}},
    {'type': 'mark', 'id': None, 'request': 'q', 'payload': {'transition': 'accepted'}},
    {'type': 'message', 'id': None, 'request': 'q', 'payload': {}},
    {'type': 'result', 'id': None, 'request': 'q', 'payload': {'outcome': 'ok'}},
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
    json.dumps({'type': 'result', 'request': 5, 'payload': {'outcome': 'ok'}}).encode('utf-8'),
    json.dumps(
      {'type': 'request', 'id': 'i', 'payload': {'kind': 'ping', 'args': {}}, 'v': 1}
    ).encode('utf-8'),
  ],
)
def test_from_bytes_malformed_raises(raw):
  with pytest.raises(ProtocolError):
    Message.from_bytes(raw)
