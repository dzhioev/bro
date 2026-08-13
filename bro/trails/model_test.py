from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from bro.trails.model import BlazeRequest, validate_end


def _wire_request(**overrides):
  data = {
    'harness': 'bro',
    'version': 'test',
    'interactive': False,
    'surface': 'ask',
    'body': {'records': []},
    'native': {'llm': {}},
    'bro': 'dev',
  }
  data.update(overrides)
  return data


def test_blaze_request_round_trips_wire_data_and_is_frozen():
  request = BlazeRequest.from_wire(_wire_request())

  assert request.to_wire() == _wire_request()
  mutable_request: Any = request
  with pytest.raises(FrozenInstanceError):
    mutable_request.surface = 'cw'


@pytest.mark.parametrize(
  'data, message',
  [
    ({**_wire_request(), 'extra': True}, 'unknown fields'),
    ({key: value for key, value in _wire_request().items() if key != 'native'}, 'missing fields'),
    (_wire_request(hold='sometimes'), 'hold must be one of'),
    (_wire_request(forked_from={'trail_id': 'parent'}), 'invalid pointer shape'),
    (_wire_request(location={'is_container': 'yes'}), 'location.is_container must be a bool'),
  ],
)
def test_blaze_request_rejects_invalid_wire_data(data, message):
  with pytest.raises(ValueError, match=message):
    BlazeRequest.from_wire(data)


@pytest.mark.parametrize(
  'reason, detail',
  [('ok', None), ('ok', ''), ('raised', 'blocked'), ('error', 'failed')],
)
def test_validate_end_accepts_writer_outcomes(reason, detail):
  validate_end(reason, detail)


@pytest.mark.parametrize(
  'reason, detail, message',
  [
    ('lost', None, 'reason must be one of'),
    ('raised', None, 'detail is required'),
    ('error', '', 'detail is required'),
    ('ok', 1, 'detail must be a string or null'),
  ],
)
def test_validate_end_rejects_invalid_outcomes(reason, detail, message):
  with pytest.raises(ValueError, match=message):
    validate_end(reason, detail)
