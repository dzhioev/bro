import pytest

from bro.base.condition import (
  ConditionError,
  SetVariable,
  StringVariable,
  Variables,
  iff,
  select,
  var,
  when,
)


def _harness(value: str) -> Variables:
  return {'harness': StringVariable(value, domain=frozenset({'bro', 'claude'}))}


def _creds(*present: str, declared: tuple[str, ...] = ('openai', 'github')) -> Variables:
  return {'creds': SetVariable(frozenset(present), universe=frozenset(declared))}


class TestEquals:
  def test_variable_against_literal(self):
    condition = var('harness') == 'bro'
    assert condition.evaluate(_harness('bro')) is True
    assert condition.evaluate(_harness('claude')) is False

  def test_reversed_operands(self):
    condition = var('harness') == var('harness')
    assert condition.evaluate(_harness('bro')) is True

  def test_boolean_variables(self):
    condition = var('flag') == var('other')
    assert condition.evaluate({'flag': True, 'other': True}) is True
    assert condition.evaluate({'flag': True, 'other': False}) is False

  def test_unknown_variable_raises_listing_known(self):
    with pytest.raises(ConditionError, match='unknown variable #nope.*known: harness'):
      (var('nope') == 'x').evaluate(_harness('bro'))

  def test_literal_outside_domain_raises(self):
    with pytest.raises(ConditionError, match='domain'):
      (var('harness') == 'bor').evaluate(_harness('bro'))

  def test_set_in_equality_raises(self):
    with pytest.raises(ConditionError, match='use contains'):
      (var('creds') == 'openai').evaluate(_creds())

  def test_boolean_against_string_raises(self):
    with pytest.raises(ConditionError, match='boolean'):
      (var('flag') == 'bro').evaluate({'flag': True})

  def test_non_operand_comparand_raises_at_declaration(self):
    with pytest.raises(ConditionError, match='cannot compare #harness against int'):
      _ = var('harness') == 5

  def test_not_equal_raises_at_declaration(self):
    with pytest.raises(ConditionError, match='no != form'):
      _ = var('harness') != 'bro'


class TestContains:
  def test_membership(self):
    condition = var('creds').contains('openai')
    assert condition.evaluate(_creds('openai')) is True
    assert condition.evaluate(_creds()) is False

  def test_variable_element(self):
    variables: Variables = {**_harness('bro'), 'creds': SetVariable(frozenset({'bro'}))}
    assert var('creds').contains(var('harness')).evaluate(variables) is True

  def test_outside_universe_raises(self):
    with pytest.raises(ConditionError, match='universe'):
      var('creds').contains('typo').evaluate(_creds('openai'))

  def test_predicate_members_probe_only_tested_names(self):
    probed: list[str] = []

    def membership(name: str) -> bool:
      probed.append(name)
      return name == 'openai'

    variables: Variables = {
      'creds': SetVariable(membership, universe=frozenset({'openai', 'github'}))
    }
    assert var('creds').contains('openai').evaluate(variables) is True
    assert probed == ['openai']

  def test_non_set_container_raises(self):
    with pytest.raises(ConditionError, match='not a set'):
      var('harness').contains('openai').evaluate(_harness('bro'))

  def test_non_string_element_raises(self):
    variables: Variables = {**_creds('openai'), 'flag': True}
    with pytest.raises(ConditionError, match='not a string'):
      var('creds').contains(var('flag')).evaluate(variables)

  def test_in_operator_raises_at_declaration(self):
    with pytest.raises(ConditionError, match='use #creds.contains'):
      _ = 'openai' in var('creds')


class TestConditionGuards:
  def test_condition_has_no_truth_value(self):
    condition = var('harness') == 'bro'
    with pytest.raises(ConditionError, match='no truth value'):
      bool(condition)

  def test_equals_renders_directive_form(self):
    assert str(var('harness') == 'claude') == '#harness = claude'

  def test_contains_renders_directive_form(self):
    assert str(var('creds').contains('openai')) == '#creds contains openai'


class TestSelect:
  def test_plain_entries_pass_through(self):
    assert select(['a', 'b'], {}) == ['a', 'b']

  def test_bool_when_is_a_constant(self):
    assert select([when(True, 'a'), when(False, 'b'), 'c'], {}) == ['a', 'c']

  def test_when_decides_at_select_time(self):
    entries = [when(var('harness') == 'bro', 'devtools'), 'tools']
    assert select(entries, _harness('bro')) == ['devtools', 'tools']
    assert select(entries, _harness('claude')) == ['tools']

  def test_condition_error_propagates(self):
    with pytest.raises(ConditionError, match='unknown variable #wire'):
      select([when(var('wire') == 'bare', 'x')], _harness('bro'))


class TestIff:
  def test_first_holding_condition_wins(self):
    entry = iff(var('harness') == 'bro', 'toolset', var('harness') == 'claude', 'builtin')
    assert select([entry], _harness('bro')) == ['toolset']
    assert select([entry], _harness('claude')) == ['builtin']

  def test_else_item_selected_when_nothing_holds(self):
    entry = iff(var('harness') == 'claude', 'builtin', 'fallback')
    assert select([entry], _harness('bro')) == ['fallback']

  def test_exhausted_without_else_raises_listing_conditions(self):
    entry = iff(var('harness') == 'claude', 'builtin')
    with pytest.raises(ConditionError, match=r'iff\(#harness = claude\).*no else'):
      select([entry], _harness('bro'))

  def test_all_conditions_validated_even_after_a_match(self):
    entry = iff(var('harness') == 'bro', 'toolset', var('typo') == 'x', 'other')
    with pytest.raises(ConditionError, match='unknown variable #typo'):
      select([entry], _harness('bro'))

  def test_non_condition_in_condition_position_raises_at_declaration(self):
    with pytest.raises(ConditionError, match='condition position'):
      iff(var('harness') == 'bro', 'a', 'stray-item', 'b')
