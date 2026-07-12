import pytest

from base.condition import SetVariable, StringVariable
from base.template import TemplateError, render


def _harness(value: str) -> dict:
  return {'harness': StringVariable(value, domain=frozenset({'bro', 'claude'}))}


def _creds(*present: str, declared: tuple[str, ...] = ('openai', 'github')) -> dict:
  return {'creds': SetVariable(frozenset(present), universe=frozenset(declared))}


class TestWhen:
  def test_taken_body_emits_and_untaken_drops(self):
    text = 'a{{when #harness = bro}}B{{end}}c'
    assert render(text, _harness('bro')) == 'aBc'
    assert render(text, _harness('claude')) == 'ac'

  def test_branch_directive_inside_when_raises(self):
    with pytest.raises(TemplateError, match='no branches'):
      render('{{when #harness = bro}}B{{else}}C{{end}}', _harness('bro'))

  def test_nested_when_in_untaken_body_stays_balanced(self):
    text = '{{when #harness = bro}}{{when #true = #true}}deep{{end}}{{end}}after'
    assert render(text, _harness('claude')) == 'after'

  def test_missing_end_raises(self):
    with pytest.raises(TemplateError, match='missing its'):
      render('{{when #harness = bro}}B', _harness('bro'))


class TestIf:
  def test_else_branch(self):
    text = '{{iff #harness = bro}}B{{else}}C{{end}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'C'

  def test_eliff_chain_takes_first_true(self):
    text = '{{iff #harness = bro}}B{{eliff #harness = claude}}C{{else}}X{{end}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'C'

  def test_exhausted_chain_without_else_raises(self):
    text = '{{iff #harness = claude}}C{{end}}'
    assert render(text, _harness('claude')) == 'C'
    with pytest.raises(TemplateError, match='no branch'):
      render(text, _harness('bro'))

  def test_exhaustive_fork_needs_no_assert(self):
    text = '{{iff #harness = bro}}B{{eliff #harness = claude}}C{{end}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'C'

  def test_exhausted_chain_in_untaken_branch_does_not_raise(self):
    # the outer else is not emitted under bro, so the inner fall-through
    # cannot fire — mirroring assert's non-taken behavior.
    text = '{{iff #harness = bro}}B{{else}}{{iff #true = #false}}x{{end}}{{end}}'
    assert render(text, _harness('bro')) == 'B'

  def test_reversed_operands(self):
    assert render('{{iff bro = #harness}}B{{end}}', _harness('bro')) == 'B'

  def test_literal_equality(self):
    assert render('{{iff a = a}}yes{{end}}', {}) == 'yes'
    assert render('{{iff a = b}}yes{{else}}no{{end}}', {}) == 'no'

  def test_boolean_builtins(self):
    assert render('{{iff #true = #true}}yes{{end}}', {}) == 'yes'
    assert render('{{iff #flag = #true}}on{{else}}off{{end}}', {'flag': False}) == 'off'

  def test_nested_blocks(self):
    text = '{{iff #harness = bro}}outer {{iff #true = #true}}inner{{end}}{{else}}other{{end}}'
    assert render(text, _harness('bro')) == 'outer inner'
    assert render(text, _harness('claude')) == 'other'

  def test_multiline_bodies(self):
    text = 'head\n{{iff #harness = bro}}line one\nline two\n{{else}}alt\n{{end}}tail'
    assert render(text, _harness('bro')) == 'head\nline one\nline two\ntail'
    assert render(text, _harness('claude')) == 'head\nalt\ntail'

  def test_whitespace_inside_braces(self):
    assert render('{{ iff  #harness = bro }}B{{ end }}', _harness('bro')) == 'B'


class TestMembership:
  def test_membership(self):
    text = '{{iff #creds contains openai}}summarized{{else}}raw{{end}}'
    assert render(text, _creds('openai')) == 'summarized'
    assert render(text, _creds()) == 'raw'

  def test_variable_element(self):
    variables = {**_harness('bro'), 'creds': SetVariable(frozenset({'bro'}), universe=None)}
    assert render('{{iff #creds contains #harness}}yes{{end}}', variables) == 'yes'

  def test_outside_universe_raises(self):
    with pytest.raises(TemplateError, match='universe'):
      render('{{iff #creds contains typo}}x{{end}}', _creds('openai'))

  def test_open_universe_allows_any_element(self):
    variables = {'creds': SetVariable(frozenset({'a'}), universe=None)}
    assert render('{{iff #creds contains zzz}}yes{{else}}no{{end}}', variables) == 'no'

  def test_predicate_members_probe_only_tested_names(self):
    probed: list[str] = []

    def membership(name: str) -> bool:
      probed.append(name)
      return name == 'openai'

    variables = {'creds': SetVariable(membership, universe=frozenset({'openai', 'github'}))}
    text = '{{iff #creds contains openai}}yes{{end}}'
    assert render(text, variables) == 'yes'
    assert probed == ['openai']

  def test_non_set_container_raises(self):
    with pytest.raises(TemplateError, match='not a set'):
      render('{{iff #harness contains openai}}x{{else}}y{{end}}', _harness('bro'))

  def test_contains_requires_surrounding_whitespace(self):
    with pytest.raises(TemplateError, match='malformed condition'):
      render('{{iff acontainsb}}x{{end}}', {})


class TestAssert:
  def test_holds_renders_nothing(self):
    assert render('a{{assert #harness = bro}}b', _harness('bro')) == 'ab'

  def test_fails_raises(self):
    with pytest.raises(TemplateError, match='assertion failed'):
      render('{{assert #harness = claude}}', _harness('bro'))

  def test_in_untaken_branch_does_not_fire(self):
    text = '{{iff #harness = bro}}B{{else}}{{assert #true = #false}}C{{end}}'
    assert render(text, _harness('bro')) == 'B'


class TestValidation:
  def test_unknown_variable_raises(self):
    with pytest.raises(TemplateError, match='unknown variable #nope'):
      render('{{iff #nope = x}}y{{end}}', {})

  def test_literal_outside_domain_raises(self):
    with pytest.raises(TemplateError, match='domain'):
      render('{{iff #harness = bor}}y{{end}}', _harness('bro'))

  def test_untaken_branch_condition_still_validated(self):
    with pytest.raises(TemplateError, match='domain'):
      render('{{iff #harness = bro}}B{{eliff #harness = claud}}C{{end}}', _harness('bro'))

  def test_set_in_equality_raises(self):
    with pytest.raises(TemplateError, match='use contains'):
      render('{{iff #creds = openai}}y{{end}}', _creds())

  def test_boolean_against_string_raises(self):
    with pytest.raises(TemplateError, match='boolean'):
      render('{{iff #true = bro}}y{{end}}', {})

  def test_malformed_condition_raises(self):
    with pytest.raises(TemplateError, match='malformed condition'):
      render('{{iff harness}}y{{end}}', {})

  def test_retired_membership_symbol_is_malformed(self):
    with pytest.raises(TemplateError, match='malformed condition'):
      render('{{iff openai ∈ #creds}}x{{end}}', _creds('openai'))

  def test_missing_end_raises(self):
    with pytest.raises(TemplateError, match='missing its'):
      render('{{iff a = a}}y', {})

  def test_retired_endif_is_not_a_terminator(self):
    with pytest.raises(TemplateError, match='missing its'):
      render('{{iff a = a}}y{{endif}}', {})

  def test_stray_end_raises(self):
    with pytest.raises(TemplateError, match='without a matching'):
      render('y{{end}}', {})

  def test_eliff_after_else_raises(self):
    with pytest.raises(TemplateError, match='after'):
      render('{{iff a = b}}x{{else}}y{{eliff a = a}}z{{end}}', {})

  def test_else_with_argument_raises(self):
    with pytest.raises(TemplateError, match='takes no argument'):
      render('{{iff a = a}}x{{else garbage}}y{{end}}', {})

  def test_end_with_argument_raises(self):
    with pytest.raises(TemplateError, match='takes no argument'):
      render('{{when a = a}}x{{end garbage}}', {})

  def test_shadowing_builtins_raises(self):
    with pytest.raises(TemplateError, match='shadow'):
      render('x', {'true': True})


class TestLiteralText:
  def test_plain_text_unchanged(self):
    assert render('no directives here', {}) == 'no directives here'

  def test_non_directive_braces_pass_through(self):
    text = 'code sample: f"{{x}}" and {{ not_a_keyword }}'
    assert render(text, {}) == text
