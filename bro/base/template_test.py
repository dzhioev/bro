from typing import Optional

import pytest

from bro.base.condition import SetVariable, StringVariable
from bro.base.template import TemplateError, render


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


class TestInsert:
  def test_emits_the_string_variable_value(self):
    assert render('from {{insert #source}}!', {'source': StringVariable('tmdb')}) == 'from tmdb!'

  def test_emits_only_in_taken_branch(self):
    text = '{{iff #harness = bro}}B{{else}}{{insert #harness}}{{end}}'
    assert render(text, _harness('bro')) == 'B'
    assert render(text, _harness('claude')) == 'claude'

  def test_unknown_variable_raises_even_in_non_taken_branch(self):
    with pytest.raises(TemplateError, match='unknown variable #nope'):
      render('{{iff #harness = bro}}B{{else}}{{insert #nope}}{{end}}', _harness('bro'))

  def test_set_variable_raises(self):
    with pytest.raises(TemplateError, match='no text form'):
      render('{{insert #creds}}', _creds('openai'))

  def test_boolean_raises(self):
    with pytest.raises(TemplateError, match='no text form'):
      render('{{insert #true}}', {})

  def test_literal_argument_raises(self):
    with pytest.raises(TemplateError, match='takes a variable reference'):
      render('{{insert source}}', {'source': StringVariable('tmdb')})

  def test_inside_unevaluated_include_does_not_resolve(self):
    files = {'x': '{{insert #foreign}}'}
    text = '{{iff #harness = bro}}B{{else}}{{include x}}{{end}}'
    assert render(text, _harness('bro'), _resolver(files)) == 'B'


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


def _resolver(files: dict[str, str], loaded: Optional[list[str]] = None):
  def resolve(name: str) -> str:
    if loaded is not None:
      loaded.append(name)
    if name not in files:
      raise FileNotFoundError(name)
    return files[name]

  return resolve


class TestInclude:
  def test_splices_rendered_with_includer_variables(self):
    files = {'x': '{{iff #harness = bro}}B{{else}}C{{end}}'}
    text = 'a {{include x}} c'
    assert render(text, _harness('bro'), _resolver(files)) == 'a B c'
    assert render(text, _harness('claude'), _resolver(files)) == 'a C c'

  def test_nested_includes(self):
    files = {'a': 'a[{{include b}}]', 'b': 'b'}
    assert render('{{include a}}', {}, _resolver(files)) == 'a[b]'

  def test_dotted_and_slashed_names(self):
    files = {'shared/00-block.md': 'block'}
    assert render('{{include shared/00-block.md}}', {}, _resolver(files)) == 'block'

  def test_untaken_branch_loads_but_does_not_emit(self):
    loaded: list[str] = []
    files = {'x': 'body'}
    text = '{{when #harness = bro}}{{include x}}{{end}}after'
    assert render(text, _harness('claude'), _resolver(files, loaded)) == 'after'
    assert loaded == ['x']

  def test_untaken_include_directives_do_not_evaluate(self):
    # the included file references a variable this surface never defines and
    # carries a failing assert; neither fires while the include is not emitted.
    files = {'x': '{{iff #foreign = y}}z{{end}}{{assert #true = #false}}'}
    text = '{{when #harness = bro}}{{include x}}{{end}}'
    assert render(text, _harness('claude'), _resolver(files)) == ''
    with pytest.raises(TemplateError, match='unknown variable #foreign'):
      render(text, _harness('bro'), _resolver(files))

  def test_untaken_include_still_structurally_parsed(self):
    files = {'x': '{{when a = a}}unterminated'}
    text = '{{when #harness = bro}}{{include x}}{{end}}'
    with pytest.raises(TemplateError, match='missing its'):
      render(text, _harness('claude'), _resolver(files))

  def test_untaken_include_condition_still_syntax_checked(self):
    files = {'x': '{{iff nonsense}}y{{end}}'}
    text = '{{when #harness = bro}}{{include x}}{{end}}'
    with pytest.raises(TemplateError, match='malformed condition'):
      render(text, _harness('claude'), _resolver(files))

  def test_transitive_load_through_untaken_include(self):
    loaded: list[str] = []
    files = {'a': '{{include b}}', 'b': 'deep'}
    text = '{{when #harness = bro}}{{include a}}{{end}}'
    assert render(text, _harness('claude'), _resolver(files, loaded)) == ''
    assert loaded == ['a', 'b']

  def test_unknown_name_raises(self):
    with pytest.raises(TemplateError, match='failed to load'):
      render('{{include missing}}', {}, _resolver({}))

  def test_unknown_name_in_untaken_branch_raises(self):
    text = '{{when #harness = bro}}{{include missing}}{{end}}'
    with pytest.raises(TemplateError, match='failed to load'):
      render(text, _harness('claude'), _resolver({}))

  def test_error_names_the_include(self):
    files = {'x': '{{iff a = a}}unterminated'}
    with pytest.raises(TemplateError, match='in .*include x'):
      render('{{include x}}', {}, _resolver(files))

  def test_cycle_raises(self):
    files = {'a': '{{include b}}', 'b': '{{include a}}'}
    with pytest.raises(TemplateError, match='include cycle: a -> b -> a'):
      render('{{include a}}', {}, _resolver(files))

  def test_self_include_raises(self):
    files = {'a': '{{include a}}'}
    with pytest.raises(TemplateError, match='include cycle: a -> a'):
      render('{{include a}}', {}, _resolver(files))

  def test_diamond_is_not_a_cycle(self):
    files = {'a': '{{include c}}', 'b': '{{include c}}', 'c': 'C'}
    assert render('{{include a}}{{include b}}', {}, _resolver(files)) == 'CC'

  def test_no_resolver_raises(self):
    with pytest.raises(TemplateError, match='no include resolver'):
      render('{{include x}}', {})

  def test_no_resolver_raises_in_untaken_branch(self):
    text = '{{when #harness = bro}}{{include x}}{{end}}'
    with pytest.raises(TemplateError, match='no include resolver'):
      render(text, _harness('claude'))

  def test_missing_name_raises(self):
    with pytest.raises(TemplateError, match='takes a name'):
      render('{{include}}', {}, _resolver({}))

  def test_multi_token_argument_raises(self):
    with pytest.raises(TemplateError, match='takes a name'):
      render('{{include two names}}', {}, _resolver({}))


class TestLiteralText:
  def test_plain_text_unchanged(self):
    assert render('no directives here', {}) == 'no directives here'

  def test_non_directive_braces_pass_through(self):
    text = 'code sample: f"{{x}}" and {{ not_a_keyword }}'
    assert render(text, {}) == text

  def test_keyword_prefixed_word_stays_literal(self):
    assert render('{{includes x}}', {}) == '{{includes x}}'
