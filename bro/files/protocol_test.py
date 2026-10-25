import io

import pytest

from bro.files.protocol import decode_json, encode_frame, read_frame


def test_frames_are_utf8_and_read_exactly_one_message():
  value = {'text': 'Кириллица'}
  data = encode_frame(value)
  assert 'Кириллица'.encode() in data
  stream = io.BytesIO(data + b'snapshot')
  assert read_frame(stream, len(data)) == value
  assert stream.read() == b'snapshot'


def test_frame_reader_refuses_oversized_length_without_consuming_body():
  stream = io.BytesIO((10000).to_bytes(8, 'big') + b'body')
  with pytest.raises(ValueError, match='byte limit'):
    read_frame(stream, 100)
  assert stream.read() == b'body'


@pytest.mark.parametrize('data', [b'', b'123', (5).to_bytes(8, 'big') + b'{}'])
def test_truncated_frames_fail(data):
  with pytest.raises(ValueError, match='incomplete'):
    read_frame(io.BytesIO(data), 100)


@pytest.mark.parametrize('data', [b'{"x":1,"x":2}', b'NaN', b'Infinity', b'\xff'])
def test_non_json_or_ambiguous_json_is_refused(data):
  with pytest.raises(ValueError):
    decode_json(data)
