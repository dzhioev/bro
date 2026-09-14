import pytest

import ride.session_env as session_env


class TestEnvAssignments:
  def test_assignments_map_in_order_and_keep_a_values_equals_signs(self):
    assert session_env.env_assignments(['B=x=y', 'A=', 'C=1']) == {'B': 'x=y', 'A': '', 'C': '1'}

  def test_none_means_no_additions(self):
    assert session_env.env_assignments(None) == {}

  @pytest.mark.parametrize('value', ['NOVALUE', '=1', '1BAD=x', 'A-B=x', 'A B=x'])
  def test_a_malformed_assignment_is_refused(self, value):
    with pytest.raises(ValueError, match='NAME=VALUE'):
      session_env.env_assignments([value])

  def test_a_repeated_name_is_refused(self):
    with pytest.raises(ValueError, match='names A twice'):
      session_env.env_assignments(['A=1', 'A=2'])


class TestEnvAdditions:
  def test_a_recorded_mapping_is_returned_as_its_own_dict(self):
    recorded = {'IS_SANDBOX': '1', 'EMPTY': ''}
    additions = session_env.env_additions(recorded)
    assert additions == recorded
    assert additions is not recorded

  @pytest.mark.parametrize('value', [[], ['A=1'], 'A=1', None])
  def test_anything_but_a_mapping_is_refused(self, value):
    with pytest.raises(ValueError, match='must be a mapping'):
      session_env.env_additions(value)

  @pytest.mark.parametrize('name', ['A=B', '1A', 'A B', ''])
  def test_a_name_the_cli_would_not_take_is_refused(self, name):
    with pytest.raises(ValueError, match='not an environment variable name'):
      session_env.env_additions({name: '1'})

  def test_a_non_string_value_is_refused(self):
    with pytest.raises(ValueError, match='must be a string value'):
      session_env.env_additions({'A': 1})
