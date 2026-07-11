import pytest

from base.template import SetVariable, StringVariable, TemplateError, render


def _harness(value: str) -> dict:
  return {'harness': StringVariable(value, domain=frozenset({'bro', 'claude'}))}


def _creds(*present: str, declared: tuple[str, ...] = ('openai', 'github')) -> dict:
  return {'creds': SetVariable(frozenset(present), universe=frozenset(declared))}


class TestIf:
  def test_taken_branch_emits(self):
    text = 'a{{if #harness = bro}}B{{endif}}c'
    assert render(text, _harness('bro')) == 'aBc'
    assert render(text, _harness('claude')) == 'ac'

  def test_else_branch(self):
    text = '{{if #harness = bro}}B{{else}}C{{endif}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'C'

  def test_elif_chain_takes_first_true(self):
    text = '{{if #harness = bro}}B{{elif #harness = claude}}C{{else}}X{{endif}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'C'

  def test_reversed_operands(self):
    assert render('{{if bro = #harness}}B{{endif}}', _harness('bro')) == 'B'

  def test_literal_equality(self):
    assert render('{{if a = a}}yes{{endif}}', {}) == 'yes'
    assert render('{{if a = b}}yes{{else}}no{{endif}}', {}) == 'no'

  def test_boolean_builtins(self):
    assert render('{{if #true = #true}}yes{{endif}}', {}) == 'yes'
    assert render('{{if #flag = #true}}on{{else}}off{{endif}}', {'flag': False}) == 'off'

  def test_nested_blocks(self):
    text = '{{if #harness = bro}}outer {{if #true = #true}}inner{{endif}}{{endif}}'
    assert render(text, _harness('bro')) == 'outer inner'
    assert render(text, _harness('claude')) == ''

  def test_nested_block_in_skipped_branch_stays_balanced(self):
    text = '{{if #harness = bro}}{{if #true = #true}}deep{{endif}}{{else}}other{{endif}}'
    assert render(text, _harness('claude')) == 'other'

  def test_multiline_bodies(self):
    text = 'head\n{{if #harness = bro}}line one\nline two\n{{endif}}tail'
    assert render(text, _harness('bro')) == 'head\nline one\nline two\ntail'
    assert render(text, _harness('claude')) == 'head\ntail'

  def test_whitespace_inside_braces(self):
    assert render('{{ if  #harness = bro }}B{{ endif }}', _harness('bro')) == 'B'


class TestMembership:
  def test_membership(self):
    text = '{{if openai ∈ #creds}}summarized{{else}}raw{{endif}}'
    assert render(text, _creds('openai')) == 'summarized'
    assert render(text, _creds()) == 'raw'

  def test_variable_element(self):
    variables = {**_harness('bro'), 'creds': SetVariable(frozenset({'bro'}), universe=None)}
    assert render('{{if #harness ∈ #creds}}yes{{endif}}', variables) == 'yes'

  def test_outside_universe_raises(self):
    with pytest.raises(TemplateError, match='universe'):
      render('{{if typo ∈ #creds}}x{{endif}}', _creds('openai'))

  def test_open_universe_allows_any_element(self):
    variables = {'creds': SetVariable(frozenset({'a'}), universe=None)}
    assert render('{{if zzz ∈ #creds}}yes{{else}}no{{endif}}', variables) == 'no'

  def test_predicate_members_probe_only_tested_names(self):
    probed: list[str] = []

    def membership(name: str) -> bool:
      probed.append(name)
      return name == 'openai'

    variables = {'creds': SetVariable(membership, universe=frozenset({'openai', 'github'}))}
    text = '{{if openai ∈ #creds}}yes{{endif}}'
    assert render(text, variables) == 'yes'
    assert probed == ['openai']

  def test_non_set_right_side_raises(self):
    with pytest.raises(TemplateError, match='not a set'):
      render('{{if openai ∈ #harness}}x{{endif}}', _harness('bro'))


class TestAssert:
  def test_holds_renders_nothing(self):
    assert render('a{{assert #harness = bro}}b', _harness('bro')) == 'ab'

  def test_fails_raises(self):
    with pytest.raises(TemplateError, match='assertion failed'):
      render('{{assert #harness = claude}}', _harness('bro'))

  def test_in_untaken_branch_does_not_fire(self):
    text = '{{if #harness = bro}}B{{else}}{{assert #harness = claude}}C{{endif}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'C'


class TestValidation:
  def test_unknown_variable_raises(self):
    with pytest.raises(TemplateError, match='unknown variable #nope'):
      render('{{if #nope = x}}y{{endif}}', {})

  def test_literal_outside_domain_raises(self):
    with pytest.raises(TemplateError, match='domain'):
      render('{{if #harness = bor}}y{{endif}}', _harness('bro'))

  def test_untaken_branch_condition_still_validated(self):
    with pytest.raises(TemplateError, match='domain'):
      render('{{if #harness = bro}}B{{elif #harness = claud}}C{{endif}}', _harness('bro'))

  def test_set_in_equality_raises(self):
    with pytest.raises(TemplateError, match='use ∈'):
      render('{{if #creds = openai}}y{{endif}}', _creds())

  def test_boolean_against_string_raises(self):
    with pytest.raises(TemplateError, match='boolean'):
      render('{{if #true = bro}}y{{endif}}', {})

  def test_malformed_condition_raises(self):
    with pytest.raises(TemplateError, match='malformed condition'):
      render('{{if harness}}y{{endif}}', {})

  def test_missing_endif_raises(self):
    with pytest.raises(TemplateError, match='missing its'):
      render('{{if a = a}}y', {})

  def test_stray_endif_raises(self):
    with pytest.raises(TemplateError, match='without a matching'):
      render('y{{endif}}', {})

  def test_elif_after_else_raises(self):
    with pytest.raises(TemplateError, match='after'):
      render('{{if a = b}}x{{else}}y{{elif a = a}}z{{endif}}', {})

  def test_else_with_argument_raises(self):
    with pytest.raises(TemplateError, match='takes no argument'):
      render('{{if a = a}}x{{else garbage}}y{{endif}}', {})

  def test_shadowing_builtins_raises(self):
    with pytest.raises(TemplateError, match='shadow'):
      render('x', {'true': True})


class TestLiteralText:
  def test_plain_text_unchanged(self):
    assert render('no directives here', {}) == 'no directives here'

  def test_non_directive_braces_pass_through(self):
    text = 'code sample: f"{{x}}" and {{ not_a_keyword }}'
    assert render(text, {}) == text
