import json

import pytest

from ride import pending_launch


def _record(**overrides) -> pending_launch.PendingLaunch:
  values = {
    'token': 'TOK-1',
    'runtime': '/runtime',
    'port': 7321,
    'channel_token': 'channel-token',
    'type': 'bro',
    'talk': ('worker.say',),
    'owner_tree': '/workspace',
    'env': {'ONE': '1'},
    'extension': {'target': 'dev'},
  }
  values.update(overrides)
  return pending_launch.PendingLaunch(**values)


@pytest.fixture(autouse=True)
def runtime_reference(monkeypatch):
  monkeypatch.setattr('ride.runtime_bundle.runtime_root_from_reference', lambda value: None)


def test_pending_launch_round_trips(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  record = _record()
  pending_launch.write(record)
  assert pending_launch.peek(record.token) == record


def test_claim_is_one_shot_and_records_the_workspace(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  pending_launch.write(_record())
  assert pending_launch.claim('TOK-1', workspace='manual-worker') == _record()
  assert pending_launch.claimed_workspace('TOK-1') == 'manual-worker'
  with pytest.raises(pending_launch.UnknownToken):
    pending_launch.claim('TOK-1', workspace='another-worker')


def test_discard_removes_pending_and_claimed_records(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  pending_launch.write(_record())
  pending_launch.claim('TOK-1', workspace='manual-worker')
  pending_launch.discard('TOK-1')
  pending_launch.discard('TOK-1')
  assert pending_launch.claimed_workspace('TOK-1') is None
  with pytest.raises(pending_launch.UnknownToken):
    pending_launch.peek('TOK-1')


def test_record_token_must_match_its_file(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  pending_launch.write(_record())
  pending_launch._path('TOK-2').parent.mkdir(parents=True, exist_ok=True)
  pending_launch._path('TOK-2').write_text(pending_launch._path('TOK-1').read_text())
  with pytest.raises(ValueError, match='names token'):
    pending_launch.peek('TOK-2')


@pytest.mark.parametrize(
  ('field', 'value', 'message'),
  [
    ('type', 'Bro', 'invalid worker type'),
    ('talk', ['worker.shout'], 'unknown talk right'),
    ('owner_tree', '', 'owner tree'),
    ('env', ['not', 'an', 'object'], 'env additions'),
    ('extension', [], 'extension object'),
  ],
)
def test_malformed_records_fail(tmp_path, monkeypatch, field, value, message):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  pending_launch.write(_record())
  data = json.loads(pending_launch._path('TOK-1').read_text())
  data[field] = value
  pending_launch._path('TOK-1').write_text(json.dumps(data))
  with pytest.raises(ValueError, match=message):
    pending_launch.peek('TOK-1')


def test_runtime_reference_reads_only_the_bootstrap_field(tmp_path, monkeypatch):
  monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
  pending_launch.write(_record())
  data = json.loads(pending_launch._path('TOK-1').read_text())
  data['extension'] = ['not validated here']
  pending_launch._path('TOK-1').write_text(json.dumps(data))
  assert pending_launch.runtime_reference('TOK-1') == '/runtime'
