import json

import pytest

from bro.broker.brotocol import PROTOCOL_REVISION
from ride import pending_summon


def _record(**overrides) -> pending_summon.PendingSummon:
  return pending_summon.PendingSummon(
    **{
      'token': 'TOK-1',
      'protocol_revision': PROTOCOL_REVISION,
      'port': 7321,
      'channel_token': 'tk',
      'target': 'dev',
      'prompt': 'pair on this',
      'parent_workspace': '/workspaces/parent/tree',
      'may_summon': ('bro',),
      'grant': ('aws',),
      'revoke': (),
      'summoner': {'trail_id': 'T1'},
      **overrides,
    }
  )


def test_peek_round_trips_the_record(tmp_path):
  record = _record(into='release')
  pending_summon.write(record)
  assert pending_summon.peek('TOK-1') == record
  # a peek does not consume
  assert pending_summon.peek('TOK-1') == record


def test_claim_consumes_and_a_second_claim_fails(tmp_path):
  pending_summon.write(_record())
  assert pending_summon.claim('TOK-1', workspace='my-manual') == _record()
  with pytest.raises(pending_summon.UnknownToken):
    pending_summon.claim('TOK-1', workspace='my-manual')


def test_claim_records_the_launch_workspace(tmp_path):
  pending_summon.write(_record())
  assert pending_summon.claimed_workspace('TOK-1') is None
  pending_summon.claim('TOK-1', workspace='my-manual')
  assert pending_summon.claimed_workspace('TOK-1') == 'my-manual'


def test_an_unusable_claimed_workspace_name_is_refused(tmp_path):
  pending_summon.write(_record())
  pending_summon.claim('TOK-1', workspace='not/a workspace')
  with pytest.raises(ValueError, match='no usable workspace name'):
    pending_summon.claimed_workspace('TOK-1')


def test_unknown_token_names_the_possible_causes(tmp_path):
  with pytest.raises(pending_summon.UnknownToken, match='never registered, already claimed'):
    pending_summon.peek('TOK-9')


def test_discard_is_idempotent_and_drops_the_claimed_record(tmp_path):
  pending_summon.write(_record())
  pending_summon.claim('TOK-1', workspace='my-manual')
  pending_summon.discard('TOK-1')
  pending_summon.discard('TOK-1')
  assert pending_summon.claimed_workspace('TOK-1') is None
  with pytest.raises(pending_summon.UnknownToken):
    pending_summon.peek('TOK-1')


def test_a_record_naming_another_token_is_refused(tmp_path):
  pending_summon.write(_record())
  source = pending_summon._path('TOK-1')
  pending_summon._path('TOK-2').write_text(source.read_text())
  with pytest.raises(ValueError, match="names token 'TOK-1'"):
    pending_summon.peek('TOK-2')


def test_a_record_without_a_protocol_revision_is_refused(tmp_path):
  pending_summon.write(_record())
  path = pending_summon._path('TOK-1')
  data = json.loads(path.read_text())
  del data['protocol_revision']
  path.write_text(json.dumps(data))

  with pytest.raises(ValueError, match='has no broker protocol revision'):
    pending_summon.claim('TOK-1', workspace='my-manual')
  assert path.exists()


def test_a_record_from_another_protocol_revision_is_refused(tmp_path):
  pending_summon.write(_record(protocol_revision=PROTOCOL_REVISION + 1))

  with pytest.raises(ValueError, match='uses broker protocol revision'):
    pending_summon.claim('TOK-1', workspace='my-manual')
  assert pending_summon._path('TOK-1').exists()
