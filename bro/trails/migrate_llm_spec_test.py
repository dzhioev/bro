import pytest

from bro.trails import migrate_llm_spec as migrate
from bro.trails.network import HTTPStatusError

_OLD_BRO = {'type': 'chat_gpt', 'model': 'gpt-5.6-sol', 'reasoning_effort': 'high'}
_OLD_CLAUDE = {'model': 'claude-opus-5', 'effort': 'xhigh'}


class TestReplacementFor:
  def test_a_retired_type_takes_its_current_name(self):
    assert migrate.replacement_for('bro', _OLD_BRO) == {
      'type': 'openai',
      'model': 'gpt-5.6-sol',
      'reasoning_effort': 'high',
    }

  def test_a_claude_recipe_gains_only_its_discriminator(self):
    # fast_mode is not synthesized: no writer recorded it, and from_dict defaults it
    assert migrate.replacement_for('claude', _OLD_CLAUDE) == {
      'type': 'claude-code',
      'model': 'claude-opus-5',
      'effort': 'xhigh',
    }

  @pytest.mark.parametrize(
    'recipe',
    [
      {'type': 'openai', 'model': 'gpt-5.6-terra'},
      {'type': 'echo', 'model': 'echo'},
      {'type': 'claude-code', 'model': 'claude-opus-5'},
    ],
  )
  def test_a_current_recipe_is_left_alone(self, recipe):
    assert migrate.replacement_for('bro', recipe) is None

  @pytest.mark.parametrize('recipe', [None, {}, {'effort': 'high'}, 'nonsense'])
  def test_a_recipe_with_no_model_is_left_alone(self, recipe):
    assert migrate.replacement_for('claude', recipe) is None

  def test_a_typeless_bro_recipe_is_not_guessed(self):
    # only the claude harness tells us what it ran under
    assert migrate.replacement_for('bro', {'model': 'gpt-5'}) is None


class _FakeClient:
  def __init__(self, headers, fail=None):
    self._headers = headers
    self.repairs: list[tuple] = []
    self._fail = fail or {}

  def iter_trails(self):
    return iter(self._headers)

  def repair_llm_spec(self, trail_id, expected, replacement):
    error = self._fail.get(trail_id)
    if error is not None:
      raise error
    self.repairs.append((trail_id, expected, replacement))
    return {'trail_id': trail_id}


def _header(trail_id, harness, llm):
  return {'id': trail_id, 'harness': harness, 'native': {'llm': llm}}


class TestMigrate:
  def test_a_dry_run_touches_nothing(self):
    client = _FakeClient([_header('a', 'bro', _OLD_BRO)])
    tally = migrate.migrate(client, apply=False, limit=None)
    assert (tally.migrated, client.repairs) == (1, [])

  def test_each_class_is_counted_and_repaired(self):
    client = _FakeClient(
      [
        _header('a', 'bro', _OLD_BRO),
        _header('b', 'claude', _OLD_CLAUDE),
        _header('c', 'bro', {'type': 'echo', 'model': 'echo'}),
        _header('d', 'claude', None),
      ]
    )
    tally = migrate.migrate(client, apply=True, limit=None)
    assert (tally.scanned, tally.migrated, tally.current, tally.skipped) == (4, 2, 1, 1)
    assert [trail_id for trail_id, _, _ in client.repairs] == ['a', 'b']

  def test_the_repair_states_the_value_it_read(self):
    # the conditional write is what makes a re-run safe, so the expected value
    # must be exactly what this pass saw
    client = _FakeClient([_header('a', 'bro', _OLD_BRO)])
    migrate.migrate(client, apply=True, limit=None)
    _, expected, replacement = client.repairs[0]
    assert expected == _OLD_BRO
    assert replacement['type'] == 'openai'

  def test_a_conflict_is_counted_not_failed(self):
    client = _FakeClient(
      [_header('a', 'bro', _OLD_BRO)], fail={'a': HTTPStatusError(409, 'changed')}
    )
    tally = migrate.migrate(client, apply=True, limit=None)
    assert (tally.conflicts, tally.failures) == (1, [])

  def test_another_failure_is_collected_and_the_pass_continues(self):
    client = _FakeClient(
      [_header('a', 'bro', _OLD_BRO), _header('b', 'bro', _OLD_BRO)],
      fail={'a': HTTPStatusError(500, 'boom')},
    )
    tally = migrate.migrate(client, apply=True, limit=None)
    assert len(tally.failures) == 1 and tally.migrated == 1

  def test_limit_stops_the_pass(self):
    client = _FakeClient([_header(str(n), 'bro', _OLD_BRO) for n in range(5)])
    assert migrate.migrate(client, apply=True, limit=2).scanned == 2
