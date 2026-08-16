import json

import pytest

from bro import summon_status

_ACTIVE = summon_status.ActiveSummon(
  request_id='R1',
  target='reviewer',
  trail_id='T1',
  summoner={'kind': 'root'},
  started_at=100.0,
)
_FINISHED = summon_status.FinishedSummon(
  request_id='R0',
  target='dev',
  trail_id='T0',
  summoner={'kind': 'root'},
  outcome='ok',
  ended_at=90.0,
)


def test_write_read_round_trip(tmp_path):
  path = tmp_path / 'sub' / 'ws.status.json'
  status = summon_status.SummonStatus(active=(_ACTIVE,), last=_FINISHED)
  summon_status.write(path, status)
  assert summon_status.read(path) == status


def test_read_is_empty_before_the_first_summon(tmp_path):
  assert summon_status.read(tmp_path / 'nothing.json') == summon_status.SummonStatus()


def test_write_leaves_no_scratch_file_behind(tmp_path):
  path = tmp_path / 'ws.status.json'
  summon_status.write(path, summon_status.SummonStatus())
  assert [p.name for p in tmp_path.iterdir()] == ['ws.status.json']


@pytest.mark.parametrize(
  'payload',
  [
    'not json{',
    json.dumps({'active': []}),  # no 'last'
    json.dumps({'active': [{'target': 'reviewer'}], 'last': None}),  # a partial record
    json.dumps({'active': [], 'last': {'outcome': 'ok', 'extra': 1}}),
  ],
)
def test_malformed_payloads_raise_value_error(tmp_path, payload):
  path = tmp_path / 'ws.status.json'
  path.write_text(payload)
  with pytest.raises(ValueError):
    summon_status.read(path)


def test_status_path_follows_the_env(monkeypatch, tmp_path):
  monkeypatch.delenv(summon_status.STATUS_ENV, raising=False)
  assert summon_status.status_path() is None
  monkeypatch.setenv(summon_status.STATUS_ENV, str(tmp_path / 'ws.status.json'))
  assert summon_status.status_path() == tmp_path / 'ws.status.json'
