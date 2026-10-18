from itertools import combinations_with_replacement, product

import pytest

from .authority import Atom, Authority, Entry, Pair, Permission
from .oracle_test_helper import descriptors, evaluate, member, powerset
from .patterns import Descriptor, PatternSet
from .rules import Declaration, Option, Refusal
from .syntax import (
  Syntax,
  parse_entry,
  parse_layer,
  parse_pattern,
  parse_permission,
  parse_values,
  render_entry,
  render_pattern,
  render_permission,
  render_values,
)
from .values import (
  Finite,
  IntegerInterval,
  Integers,
  LatticeInterval,
  Strings,
  Subsets,
  Value,
  ValueSet,
)


@pytest.fixture
def syntax():
  placements = frozenset({'boxed', 'unboxed', 'join'})
  talk = frozenset({'owner.say', 'worker.say', 'worker.question'})
  return Syntax(
    {
      'bro': Declaration(
        {
          'target': Option('scalar', Strings(cofinite=True)),
          'placement': Option('scalar', Finite(placements, placements)),
          'talk': Option('subset', Subsets(talk).universe()),
          'cred.github': Option('scalar', Strings(cofinite=True), overridable=True),
          'timeout': Option('scalar', Integers((IntegerInterval(1, 3600),))),
        }
      ),
      'webview': Declaration(
        {
          'vnc': Option('scalar', Finite(frozenset({True}), frozenset({True}))),
          'cred.cookies': Option('scalar', Strings(cofinite=True)),
          'vnc_port': Option('scalar', Integers((IntegerInterval(1024, 65535),))),
        }
      ),
      'benchmark': Declaration({}),
    },
    ('github', 'cookies'),
  )


@pytest.mark.parametrize(
  'text',
  [
    'bro',
    'webview.vnc',
    '@browser',
    '@for',
    '@to',
    '@*',
    'bro.target={browser,helper}',
    'bro.cred=github+{dev,reviewer}',
    'webview.cred=cookies+*',
    'bro.cred=github+',
    'bro.cred=github+for',
    'bro.cred=github+to',
    'bro.timeout=1..3600',
    'bro.talk=>=[worker.say]&<=[worker.say,owner.say]',
    'bro.target=!{browser,helper}',
    'bro.target="1..2"',
  ],
)
def test_permission_round_trips(syntax, text):
  permission = syntax.permission(parse_permission(text))
  rendered = render_permission(permission)
  assert syntax.permission(parse_permission(rendered)) == permission
  assert render_permission(syntax.permission(parse_permission(rendered))) == rendered


@pytest.mark.parametrize(
  'text',
  [
    '@browser',
    '@for',
    '@to',
    '@*',
    'bro{}',
    'bro{target=browser,placement?={boxed,unboxed},!talk}',
    'bro{target,timeout=60..3600}',
    'bro{target?={},talk=[]}',
    'bro{cred=github+}',
    'bro{!cred.github}',
    'bro{cred.github}',
  ],
)
def test_pattern_round_trips(syntax, text):
  pattern = syntax.pattern(parse_pattern(text))
  rendered = render_pattern(pattern)
  assert syntax.pattern(parse_pattern(rendered)) == pattern
  assert render_pattern(syntax.pattern(parse_pattern(rendered))) == rendered


@pytest.mark.parametrize(
  'text',
  [
    'webview',
    'webview for *',
    'webview to *',
    'webview for * to *',
    'webview.cred=cookies+assistant to @browser',
    'webview for webview{vnc} to @browser bro{target=helper,placement=join}',
    'bro.target={browser,helper} for bro{placement=boxed} to benchmark{}',
  ],
)
def test_entry_round_trips(syntax, text):
  entry = syntax.entry(parse_entry(text))
  rendered = render_entry(entry, syntax.declarations)
  parsed = syntax.entry(parse_entry(rendered))
  assert parsed.permission == entry.permission
  for left, right in ((parsed.pair.use, entry.pair.use), (parsed.pair.give, entry.pair.give)):
    assert left.within(right) and right.within(left)
  assert render_entry(syntax.entry(parse_entry(rendered)), syntax.declarations) == rendered


def test_qualified_entries_allow_exactly_their_clauses(syntax):
  recipient = Descriptor('bro', {'target': 'browser', 'placement': 'boxed'})
  atom = Atom('webview', 'cred.cookies', 'assistant')
  for text, use, give in [
    ('webview.cred=cookies+assistant', True, True),
    ('webview.cred=cookies+assistant for *', True, False),
    ('webview.cred=cookies+assistant to @browser', False, True),
    ('webview.cred=cookies+assistant for * to @browser', True, True),
  ]:
    authority = Authority((syntax.entry(parse_entry(text)),))
    assert evaluate(authority, atom, recipient, Descriptor('webview'), give=False) == use
    assert evaluate(authority, atom, recipient, recipient, give=True) == give


def test_recipient_defaults_exclude_joins_but_explicit_placement_and_star_allow_them(syntax):
  joined = Descriptor('bro', {'target': 'browser', 'placement': 'join'})
  for pattern in ('@browser', '@*', 'bro{}', 'bro{target=browser}'):
    assert not syntax.entry(parse_entry(f'webview to {pattern}')).pair.give.matches(joined)
  for pattern in ('*', 'bro{placement=*}', 'bro{placement}', 'bro{placement?=join}'):
    assert syntax.entry(parse_entry(f'webview to {pattern}')).pair.give.matches(joined)
  assert syntax.entry(parse_entry('webview for webview{}')).pair.use.matches(Descriptor('webview'))
  assert syntax.pattern(parse_pattern('@*')).matches(Descriptor('bro'))


def test_patterns_distinguish_unset_empty_subset_optional_and_required(syntax):
  pattern = syntax.pattern(parse_pattern('bro{target=browser,talk?=[],!timeout}'))
  assert pattern.matches(Descriptor('bro', {'target': 'browser'}))
  assert pattern.matches(Descriptor('bro', {'target': 'browser', 'talk': frozenset()}))
  assert not pattern.matches(Descriptor('bro'))
  assert not pattern.matches(
    Descriptor('bro', {'target': 'browser', 'talk': frozenset({'worker.say'})})
  )
  assert not pattern.matches(Descriptor('bro', {'target': 'browser', 'timeout': 10}))


def test_layer_caps_precede_grants_and_credential_kinds_are_not_permissions(syntax):
  layer = syntax.layer(
    parse_layer(
      grant=('github', '@browser'),
      revoke=('cookies', 'webview.vnc'),
      limit=('webview for * to @browser',),
    )
  )
  assert layer.hold == {'github'}
  assert layer.drop == {'cookies'}
  authority = layer.apply(Authority.top())
  atom = Atom('webview', 'vnc', True)
  browser = Descriptor('bro', {'target': 'browser', 'placement': 'boxed'})
  assert authority.evaluate(atom, browser, syntax.declarations).is_empty
  pair = authority.evaluate(Atom('webview'), browser, syntax.declarations)
  assert pair.use.matches(Descriptor('webview'))
  assert pair.give.matches(browser)
  assert not pair.give.matches(Descriptor('bro', {'target': 'helper'}))
  assert authority.evaluate(Atom('benchmark'), browser, syntax.declarations).use.matches(
    Descriptor('benchmark')
  )
  later = syntax.layer(parse_layer(grant=('webview.vnc',))).apply(authority)
  assert later.evaluate(atom, browser, syntax.declarations).use.matches(Descriptor('webview'))


def test_operator_caps_leave_unmentioned_permissions_alone(syntax):
  layer = syntax.layer(parse_layer(revoke=('@browser',), operator=True))
  authority = layer.apply(Authority.top())
  descriptor = Descriptor('bro', {'target': 'helper'})
  assert authority.evaluate(
    Atom('bro', 'target', 'browser'), descriptor, syntax.declarations
  ).is_empty
  assert authority.evaluate(
    Atom('bro', 'target', 'helper'), descriptor, syntax.declarations
  ).use.matches(descriptor)


def test_plain_summon_gifts_forward_recipients_per_region(syntax):
  giver = Authority(
    tuple(
      syntax.entry(parse_entry(text))
      for text in (
        'webview.cred=cookies+assistant to @browser',
        'webview.cred=cookies+other to bro{target=helper,placement=unboxed}',
      )
    )
  )
  gift = syntax.forward(parse_entry('webview.cred=cookies+{assistant,other}'), giver)
  browser = Descriptor('bro', {'target': 'browser', 'placement': 'boxed'})
  for instance in ('assistant', 'other'):
    atom = Atom('webview', 'cred.cookies', instance)
    held = giver.evaluate(atom, browser, syntax.declarations)
    forwarded = gift.evaluate(atom, browser, syntax.declarations)
    assert forwarded.give == held.give
    assert forwarded.use.matches(Descriptor('webview'))
  assistant = syntax.forward(parse_entry('webview.cred=cookies+assistant'), giver)
  syntax.rules.gift(giver, assistant, browser)
  with pytest.raises(Refusal, match='gift recipients exceed'):
    syntax.rules.gift(
      giver,
      syntax.forward(parse_entry('webview.cred=cookies+assistant for * to *'), giver),
      browser,
    )


def test_unknown_installation_names_fail_only_on_resolution(syntax):
  layers = [
    parse_layer(grant=(text,))
    for text in (
      'absent',
      'bro.absent=*',
      'bro.cred=absent+*',
      'webview to absent{}',
    )
  ]
  syntax.layer(parse_layer(grant=('@browser',)))
  for layer in layers:
    with pytest.raises(ValueError, match='unknown'):
      syntax.layer(layer)
  with pytest.raises(ValueError, match='both credential kind and mission type'):
    Syntax({'github': Declaration({})}, ('github',))


@pytest.mark.parametrize(
  'arguments',
  [
    {'grant': ('github',), 'revoke': ('github',)},
    {'grant': ('@*',), 'revoke': ('@browser',)},
    {'grant': ('bro.placement={boxed,join}',), 'revoke': ('bro.placement=join',)},
  ],
)
def test_overlapping_grants_and_revokes_are_refused(syntax, arguments):
  with pytest.raises(ValueError, match='cannot grant and revoke'):
    syntax.layer(parse_layer(**arguments))


@pytest.mark.parametrize(
  'arguments',
  [
    {'grant': ('webview',), 'operator': True},
    {'revoke': ('webview to *',)},
    {'limit': ('webview',)},
  ],
)
def test_list_structure_is_checked_without_registry_lookup(arguments):
  with pytest.raises(ValueError):
    parse_layer(**arguments)


@pytest.mark.parametrize(
  'text',
  [
    '',
    ':launch.bro',
    '@',
    'bro.cred=github',
    'bro.cred.github=dev',
    'bro.target=',
    'bro.target={a,}',
    'bro.target=a || b',
    'bro.target=(a|b',
    'bro for',
    'bro to',
    'bro to * for *',
    'bro for * for *',
    'bro to bro{target=a,target=b}',
    'bro to bro{!target=a}',
    'bro to bro{target=a,}',
    'bro.target="bad\\q"',
    'bro.target=$value',
  ],
)
def test_malformed_entries_are_refused(text):
  with pytest.raises(ValueError):
    parse_entry(text)


@pytest.mark.parametrize(
  'text',
  [
    'bro.target',
    'bro.timeout=0',
    'bro.timeout=60..',
    'webview.vnc=1',
    'bro.talk=worker.say',
    'bro.target=[browser]',
    'bro.talk=>=[absent]',
    'webview for bro{}',
    'bro for bro{cred=github+dev}',
    'github to *',
  ],
)
def test_semantically_invalid_entries_are_refused(syntax, text):
  with pytest.raises(ValueError):
    syntax.layer(parse_layer(grant=(text,)))


def test_precedence_parentheses_and_domain_relative_complements():
  domain = Strings(cofinite=True)
  assert parse_values('a|b&!b', domain) == Strings(frozenset({'a'}))
  assert parse_values('!(a|b)&!c', domain) == Strings(frozenset({'a', 'b', 'c'}), True)
  bounded = Integers((IntegerInterval(1, 5),))
  assert parse_values('!2..4', bounded) == Integers((IntegerInterval(1, 1), IntegerInterval(5, 5)))


def check_rendered_sets(sets: list[ValueSet], witnesses):
  canonical = {}
  for values in sets:
    text = render_values(values)
    parsed = parse_values(text, values.universe())
    signature = tuple(member(values, witness) for witness in witnesses)
    assert tuple(member(parsed, witness) for witness in witnesses) == signature
    assert parsed.difference(values).is_empty
    assert values.difference(parsed).is_empty
    assert render_values(parsed) == text
    assert canonical.setdefault(signature, text) == text


def test_rendered_strings_and_all_finite_families_against_oracle():
  names = ['a', 'b', '', '1..2', 'for', 'with space', 'quote"', '☀']
  sets: list[ValueSet] = [
    Strings(members, cofinite) for members in powerset(names) for cofinite in (False, True)
  ]
  check_rendered_sets(sets, [*names, 'fresh', 'another-fresh'])
  for base in (frozenset({'a', 'b', 'c'}), frozenset({True, False}), frozenset({-1, 0, 1})):
    check_rendered_sets([Finite(base, members) for members in powerset(base)], list(base))


def test_rendered_integer_results_against_oracle():
  intervals = [
    IntegerInterval(lower, upper)
    for lower in (None, -1, 0, 1)
    for upper in (-1, 0, 1, None)
    if lower is None or upper is None or lower <= upper
  ]
  sets = [
    Integers(),
    *(Integers((interval,)) for interval in intervals),
    *(Integers(pair) for pair in combinations_with_replacement(intervals, 2)),
  ]
  computed = [
    operation(left, right)
    for left, right in product(sets, repeat=2)
    for operation in (Integers.intersection, Integers.difference)
  ]
  check_rendered_sets(
    list(dict.fromkeys([*sets, *computed])), list(range(-4, 5)) + [-1000000, 1000000]
  )


def test_every_subset_set_has_one_spelling_independent_of_interval_decomposition():
  base = frozenset({'a', 'b', 'c'})
  subsets = powerset(base)
  intervals = [
    LatticeInterval(lower, upper) for lower, upper in product(subsets, repeat=2) if lower <= upper
  ]
  sets = [
    Subsets(base),
    *(Subsets(base, (interval,)) for interval in intervals),
    *(Subsets(base, pair) for pair in combinations_with_replacement(intervals, 2)),
  ]
  sets.extend(
    Subsets(base, tuple(LatticeInterval(value, value) for value in members))
    for members in powerset(subsets)
  )
  computed = [
    operation(left, right)
    for left, right in product(sets[: len(intervals) + 1], repeat=2)
    for operation in (Subsets.intersection, Subsets.difference)
  ]
  check_rendered_sets([*sets, *computed], subsets)


def test_patterns_computed_by_subtraction_round_trip_against_oracle(syntax):
  left = syntax.pattern(parse_pattern('bro{target?={browser,helper},talk?=<=[worker.say]}'))
  right = syntax.pattern(parse_pattern('bro{target=browser,talk=>=[worker.say]}'))
  witnesses = descriptors(
    'bro',
    {
      'target': ['browser', 'helper', 'fresh'],
      'talk': list[Value](powerset(['worker.say', 'owner.say'])),
    },
  )
  for pattern in left.difference(right):
    parsed = syntax.pattern(parse_pattern(render_pattern(pattern)))
    for descriptor in witnesses:
      assert pattern.matches(descriptor) == parsed.matches(descriptor)


def test_integer_one_is_not_a_flag():
  domain = Finite(frozenset({1}), frozenset({1}))
  syntax = Syntax({'job': Declaration({'number': Option('scalar', domain)})})
  with pytest.raises(ValueError, match='not a flag'):
    syntax.permission(parse_permission('job.number'))
  permission = Permission('job', 'number', domain)
  assert syntax.permission(parse_permission(render_permission(permission))) == permission


def test_empty_pair_rendering_requires_the_revoke_list():
  with pytest.raises(ValueError, match='revoke'):
    render_entry(Entry(Permission('job'), Pair()), ('job',))


def test_computed_unrestricted_bro_recipient_does_not_acquire_the_join_default(syntax):
  entry = Entry(Permission('webview'), Pair(give=PatternSet.all('bro')))
  parsed = syntax.entry(parse_entry(render_entry(entry, syntax.declarations)))
  joined = Descriptor('bro', {'target': 'browser', 'placement': 'join'})
  assert parsed.pair.give.matches(joined)
  assert parsed.pair.give.within(entry.pair.give)
  assert entry.pair.give.within(parsed.pair.give, syntax.rules.domains)


def test_empty_computed_patterns_render_without_gaining_matches(syntax):
  set_pattern = syntax.pattern(parse_pattern('bro{target}'))
  unset_pattern = syntax.pattern(parse_pattern('bro{!target}'))
  empty = set_pattern.meet(unset_pattern)
  assert empty is not None
  parsed = syntax.pattern(parse_pattern(render_pattern(empty)))
  assert parsed.is_empty
  assert parsed.matches(Descriptor('bro'), prepare=True)
  assert not parsed.matches(Descriptor('bro', {'target': 'browser'}), prepare=True)


@pytest.mark.parametrize('type', ['for', 'to'])
def test_clause_keywords_can_name_mission_types_in_entries(syntax, type):
  syntax = Syntax({**syntax.declarations, type: Declaration({})}, syntax.credential_kinds)
  for text in (
    f'bro to {type}{{}}',
    f'{type} for {type}{{}} to {type}{{}}',
    f'bro to @for {type}{{}} @to',
  ):
    entry = syntax.entry(parse_entry(text))
    rendered = render_entry(entry, syntax.declarations)
    parsed = syntax.entry(parse_entry(rendered))
    assert parsed.permission == entry.permission
    for left, right in ((parsed.pair.use, entry.pair.use), (parsed.pair.give, entry.pair.give)):
      assert left.within(right) and right.within(left)
    assert render_entry(parsed, syntax.declarations) == rendered


@pytest.mark.parametrize('instance', ['for', 'to'])
def test_credential_instance_keywords_are_values_before_clauses(syntax, instance):
  for suffix in ('', ' for *', ' to @browser', ' for * to @browser'):
    entry = syntax.entry(parse_entry(f'bro.cred=github+{instance}{suffix}'))
    assert entry.permission.values == Strings(frozenset({instance}))
    rendered = render_entry(entry, syntax.declarations)
    assert syntax.entry(parse_entry(rendered)) == entry
  empty = syntax.entry(parse_entry('bro.cred=github+ for * to @browser'))
  assert empty.permission.values == Strings(frozenset({''}))
