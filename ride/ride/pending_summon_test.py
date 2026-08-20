import pytest

from ride import pending_summon


def _record(**overrides) -> pending_summon.PendingSummon:
  return pending_summon.PendingSummon(
    **{
      'token': 'TOK-1',
      'socket': '/broker/CH.sock',
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
  pending_summon.write(tmp_path, record)
  assert pending_summon.peek(tmp_path, 'TOK-1') == record
  # a peek does not consume
  assert pending_summon.peek(tmp_path, 'TOK-1') == record


def test_claim_consumes_and_a_second_claim_fails(tmp_path):
  pending_summon.write(tmp_path, _record())
  assert pending_summon.claim(tmp_path, 'TOK-1') == _record()
  with pytest.raises(pending_summon.UnknownToken):
    pending_summon.claim(tmp_path, 'TOK-1')


def test_unknown_token_names_the_possible_causes(tmp_path):
  with pytest.raises(pending_summon.UnknownToken, match='never registered, already claimed'):
    pending_summon.peek(tmp_path, 'TOK-9')


def test_discard_is_idempotent(tmp_path):
  pending_summon.write(tmp_path, _record())
  pending_summon.discard(tmp_path, 'TOK-1')
  pending_summon.discard(tmp_path, 'TOK-1')
  with pytest.raises(pending_summon.UnknownToken):
    pending_summon.peek(tmp_path, 'TOK-1')


def test_a_record_naming_another_token_is_refused(tmp_path):
  pending_summon.write(tmp_path, _record())
  source = pending_summon._path(tmp_path, 'TOK-1')
  pending_summon._path(tmp_path, 'TOK-2').write_text(source.read_text())
  with pytest.raises(ValueError, match="names token 'TOK-1'"):
    pending_summon.peek(tmp_path, 'TOK-2')
