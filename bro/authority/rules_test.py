from dataclasses import replace
from functools import partial
from itertools import product
from random import Random

import pytest

from .authority import Atom, Authority, Cap, Entry, LazyEntry, Pair, Permission
from .oracle_test_helper import descriptors, evaluate, powerset
from .patterns import Descriptor, Pattern, PatternSet
from .rules import Admission, Declaration, Option, Override, Refusal, Request, Rules
from .values import Finite, IntegerInterval, Integers, LatticeInterval, Strings, Subsets


def model_scalar_values(*values: str) -> Finite:
  return Finite(frozenset({'a1', 'a2', 'a3', 'a4', 'a5'}), frozenset(values))


@pytest.fixture
def model():
  base = frozenset({'b1', 'b2', 'b3', 'b4'})
  all_types = PatternSet.all('T', 'U')
  all_use = PatternSet.all('T')
  recipient = PatternSet((Pattern('T', {'A': model_scalar_values('a1')}, frozenset({'A'})),))
  permission = Permission('U', 'C', Strings(frozenset({'c1'})))
  h0 = Authority(
    (
      Entry(Permission('T'), Pair(all_use)),
      Entry(Permission('T', 'A', model_scalar_values('a1', 'a2', 'a3')), Pair(all_use)),
      Entry(
        Permission('T', 'B', Subsets(base, (LatticeInterval(frozenset(), base - {'b4'}),))),
        Pair(all_use),
      ),
      Entry(Permission('T', 'F', Finite(frozenset({'on'}), frozenset({'on'}))), Pair(all_use)),
      Entry(Permission('T', 'N', Integers((IntegerInterval(1, 100),))), Pair(all_use)),
      Entry(permission, Pair(give=recipient)),
    )
  )
  child_base = Authority(
    (
      Entry(Permission('U'), Pair(PatternSet.all('U'))),
      Entry(Permission('U', 'C', Strings(frozenset({'c2'}))), Pair(PatternSet.all('U'))),
    )
  )
  calls = []

  def defaults(fixed, reading):
    assert 'N' not in fixed.values
    return {'A': 'a1', 'B': frozenset({'b1', 'b2'})}

  def variable_defaults(fixed, variable, reading):
    assert 'N' not in fixed.values
    return {'N': reading}

  def validity(descriptor):
    if 'b4' in descriptor.values['B'] and 'F' not in descriptor.values:
      raise ValueError('B containing b4 requires F')

  def authority(fixed, reading):
    assert 'N' not in fixed.values
    calls.append(fixed)
    return h0 if fixed.values['A'] == 'a2' else child_base

  rules = Rules(
    {
      'T': Declaration(
        {
          'A': Option('scalar', model_scalar_values().universe()),
          'B': Option('subset', Subsets(base).universe()),
          'F': Option('scalar', Finite(frozenset({'on'}), frozenset({'on'}))),
          'N': Option('scalar', Integers((IntegerInterval(1, 100),)), overridable=True),
        },
        defaults,
        variable_defaults,
        validity,
        authority,
      ),
      'U': Declaration({'C': Option('scalar', Strings(cofinite=True))}),
    }
  )
  return rules, h0, permission, recipient, calls, all_types


def test_model_worked_start_gift_cap_and_relay(model):
  rules, h0, permission, recipient, calls, _ = model
  gift = Authority((Entry(permission, Pair(PatternSet.all('U'))),))
  cap = Cap((Entry(Permission('U', 'C', Strings(frozenset({'c2'}))), Pair()),))
  mission = rules.start(Request('T', gift=gift, cap=cap), h0, 50)
  assert mission.descriptor.values == {'A': 'a1', 'B': frozenset({'b1', 'b2'}), 'N': 50}
  rules.start(Request('U', {'C': 'c1'}), mission.authority, 50)
  with pytest.raises(Refusal, match='use rule.*c2'):
    rules.start(Request('U', {'C': 'c2'}), mission.authority, 50)
  with pytest.raises(Refusal, match='use rule'):
    rules.start(Request('U', {'C': 'c1'}), h0, 50)
  with pytest.raises(Refusal, match='gift rule'):
    rules.start(Request('T', {'A': 'a2'}, gift), h0, 50)
  relay = Authority((Entry(permission, Pair(PatternSet.all('U'), recipient)),))
  rules.start(Request('T', gift=relay), h0, 50)
  too_wide = Authority((Entry(permission, Pair(PatternSet.all('U'), PatternSet.all('T'))),))
  with pytest.raises(Refusal, match='gift rule.*exceed'):
    rules.start(Request('T', gift=too_wide), h0, 50)
  assert calls


def test_normalization_defaults_are_checked_and_invalid_values_refused(model):
  rules, h0, _, _, _, _ = model
  with pytest.raises(Refusal, match='use rule.*a4'):
    rules.start(Request('T', {'A': 'a4'}), h0, 50)
  narrowed = h0.meet(Cap((Entry(Permission('T', 'A', model_scalar_values('a1')), Pair()),)))
  with pytest.raises(Refusal, match='use rule.*a1'):
    rules.start(Request('T'), narrowed, 50)
  rules.start(Request('T', {'A': 'a2'}), narrowed, 50)
  with pytest.raises(Refusal, match='validity check.*requires F'):
    rules.start(Request('T', {'B': frozenset({'b4'})}), h0, 50)
  for request, match in [
    (Request('missing'), 'unknown type'),
    (Request('T', {'unknown': 'a'}), 'unknown option'),
    (Request('T', {'N': 120}), 'domain check'),
    (Request('T', {'N': True}), 'domain check'),
    (Request('T', {'B': frozenset({'b5'})}), 'domain check'),
    (
      Request('T', gift=Authority((Entry(Permission('U', 'absent', Strings()), Pair()),))),
      'unknown option',
    ),
    (Request('T', cap=Cap((Entry(Permission('T', 'N', Strings()), Pair()),))), 'value-set check'),
  ]:
    with pytest.raises(Refusal, match=match):
      rules.start(request, Authority.top(), 50)


def test_joint_prepare_and_commit_check_final_values_and_current_authorities(model):
  rules, h0, permission, _, _, _ = model
  gift = Authority((Entry(permission, Pair(PatternSet.all('U'))),))
  prepared = rules.prepare(Request('T', gift=gift), h0, 50)
  with pytest.raises(Refusal, match='gift rule'):
    rules.commit(prepared, {'A': 'a3'}, h0, Authority.top())
  committed = rules.commit(
    prepared, {'A': 'a1', 'B': frozenset({'b2', 'b4'}), 'F': 'on'}, h0, Authority.top()
  )
  assert committed.operator_values == {'A', 'B', 'F'}
  assert committed.descriptor.values['B'] == {'b2', 'b4'}
  with pytest.raises(Refusal, match='use rule'):
    rules.commit(prepared, {'A': 'a1'}, Authority(), Authority.top())
  with pytest.raises(Refusal, match='use rule'):
    rules.commit(prepared, {'A': 'a1'}, h0, Authority())
  without_gift = rules.prepare(Request('T', {'A': 'a1'}), h0, 50)
  assert rules.commit(without_gift, {'A': 'a4'}, h0, Authority.top()).descriptor.values['A'] == 'a4'


def test_prepare_ignores_unset_conditions_but_checks_set_values_and_gift_give(model):
  rules, h0, permission, _, _, _ = model
  conditional = h0.meet(
    Cap(
      (
        Entry(
          Permission('T'),
          Pair(PatternSet((Pattern('T', {}, frozenset({'F'})),)), PatternSet.all('T', 'U')),
        ),
      )
    )
  )
  pending = rules.prepare(Request('T'), conditional, 50)
  with pytest.raises(Refusal, match='use rule'):
    rules.commit(pending, {}, conditional, Authority.top())
  rules.commit(pending, {'F': 'on'}, conditional, Authority.top())
  with pytest.raises(Refusal, match='use rule'):
    rules.prepare(Request('T', {'A': 'a4'}), h0, 50)
  gift = Authority((Entry(permission, Pair(give=PatternSet.all('T'))),))
  with pytest.raises(Refusal, match='gift rule'):
    rules.prepare(Request('T', gift=gift), h0, 50)


def test_override_neutrality_operator_layer_and_failed_transition_leave_input_unchanged(model):
  rules, h0, permission, _, calls, _ = model
  cap = Cap((Entry(permission, Pair()),))
  mission = rules.start(Request('T', cap=cap), h0, 50)
  prior_calls = len(calls)
  gift = Authority((Entry(permission, Pair(PatternSet.all('U'))),))
  changed = rules.override(mission, Override({'N': 80}, gift=gift), Authority.top())
  assert changed.descriptor.values['N'] == 80
  assert mission.descriptor.values['N'] == 50
  assert len(calls) == prior_calls
  rules.start(Request('U', {'C': 'c1'}), changed.authority, 50)
  denied = rules.override(changed, Override(cap=cap), Authority.top())
  with pytest.raises(Refusal, match='use rule'):
    rules.start(Request('U', {'C': 'c1'}), denied.authority, 50)
  for override, match in [
    (Override({'A': 'a2'}), 'not overridable'),
    (Override({'N': 120}), 'domain check'),
  ]:
    with pytest.raises(Refusal, match=match):
      rules.override(mission, override, Authority.top())
  with pytest.raises(Refusal, match='use rule'):
    rules.override(mission, Override({'N': 80}), Authority())
  with pytest.raises(Refusal, match='gift rule'):
    rules.override(mission, Override(gift=gift), h0.meet(cap))
  cleared = rules.override(changed, Override(clear=frozenset({'N'})), Authority.top())
  assert cleared.descriptor.values['N'] == 50
  assert 'N' not in cleared.request.values


def test_patterns_cannot_name_overridable_options_in_use_or_give(model):
  rules, _, permission, _, _, _ = model
  forbidden = PatternSet((Pattern('T', {'N': Integers((IntegerInterval(1, 50),))}),))
  for pair in [Pair(forbidden), Pair(give=forbidden)]:
    with pytest.raises(Refusal, match='authority-neutral check'):
      rules.start(
        Request('T', gift=Authority((Entry(Permission('T'), pair),))), Authority.top(), 50
      )


def test_default_callback_shape_and_return_checks_enforce_authority_neutrality(model):
  rules, _, _, _, _, _ = model
  declaration = rules.declarations['T']
  bad = Rules({'T': replace(declaration, defaults=lambda fixed, reading: {'N': 50})})
  with pytest.raises(ValueError, match='fixed defaults returned overridable'):
    bad.normalize(Request('T'), 50)
  bad = Rules(
    {'T': replace(declaration, overridable_defaults=lambda fixed, values, reading: {'A': 'a1'})}
  )
  with pytest.raises(ValueError, match='overridable defaults returned fixed'):
    bad.normalize(Request('T'), 50)


def test_admission_runs_only_after_authority_and_reports_its_name(model):
  rules, h0, _, _, _, _ = model
  calls = []

  def depth(descriptor):
    calls.append(descriptor)
    raise ValueError('chain is full')

  check = (Admission('chain length check', depth),)
  with pytest.raises(Refusal, match='use rule'):
    rules.start(Request('U', {'C': 'absent'}), h0, 50, check)
  assert not calls
  with pytest.raises(Refusal, match='chain length check.*U'):
    rules.start(Request('U', {'C': 'absent'}), Authority.top(), 50, check)
  assert len(calls) == 1


def test_use_and_gift_rules_against_independent_finite_evaluator():
  random = Random(811)
  base = frozenset({'a', 'b'})
  rules = Rules(
    {type: Declaration({'A': Option('scalar', Strings(cofinite=True))}) for type in ['T', 'U']}
  )
  cases = [
    case
    for type in rules.types
    for case in descriptors(type, {'A': ['a', 'b', 'fresh', 'another']})
  ]
  atoms = [Atom(type) for type in rules.types] + [
    Atom(type, 'A', value) for type in rules.types for value in ['a', 'b', 'fresh', 'another']
  ]

  def patterns(type=None):
    return PatternSet(
      tuple(
        Pattern(
          type or random.choice(rules.types),
          {'A': Strings(random.choice(powerset(base)), random.choice([False, True]))},
          frozenset({'A'}) if random.choice([False, True]) else frozenset(),
        )
        for _ in range(random.randrange(3))
      )
    )

  def authority():
    entries = []
    for _ in range(random.randrange(5)):
      type = random.choice(rules.types)
      permission = (
        Permission(type)
        if random.choice([False, True])
        else Permission(
          type, 'A', Strings(random.choice(powerset(base)), random.choice([False, True]))
        )
      )
      entries.append(Entry(permission, Pair(patterns(type), patterns())))
    return Authority(tuple(entries))

  def passes(callback):
    try:
      callback()
      return True
    except Refusal:
      return False

  for _ in range(100):
    starter, gift = authority().meet(Cap(authority().entries)), authority()
    for descriptor in random.sample(cases, 3):
      used = [
        Atom(descriptor.type),
        *(Atom(descriptor.type, name, value) for name, value in descriptor.values.items()),
      ]
      expected_use = all(
        evaluate(starter, atom, descriptor, descriptor, give=False) for atom in used
      )
      assert passes(partial(rules.use, starter, descriptor)) == expected_use
      expected_gift = True
      for atom in atoms:
        active = any(
          evaluate(gift, atom, descriptor, probe, give=give)
          for probe, give in product(cases, [False, True])
        )
        if active:
          expected_gift &= evaluate(starter, atom, descriptor, descriptor, give=True)
          expected_gift &= all(
            not evaluate(gift, atom, descriptor, probe, give=True)
            or evaluate(starter, atom, descriptor, probe, give=True)
            for probe in cases
          )
      assert passes(partial(rules.gift, starter, gift, descriptor)) == expected_gift


def test_unbounded_gifts_are_checked_beyond_every_named_value():
  rules = Rules({'T': Declaration({'A': Option('scalar', Strings(cofinite=True))})})
  named = Permission('T', 'A', Strings(frozenset({'a', 'b'})))
  starter = Authority((Entry(named, Pair(give=PatternSet.all('T'))),))
  gift = Authority(
    (Entry(Permission('T', 'A', Strings(cofinite=True)), Pair(PatternSet.all('T'))),)
  )
  with pytest.raises(Refusal, match='gift rule'):
    rules.gift(starter, gift, Descriptor('T'))
  with pytest.raises(Refusal, match='gift rule'):
    rules.gift(starter, Authority.top(), Descriptor('T'))


def test_lazy_entries_in_gift_rule_are_checked_symbolically():
  rules = Rules({'T': Declaration({'A': Option('scalar', Strings(cofinite=True))})})
  permission = Permission('T', 'A', Strings(cofinite=True))
  starter = Authority((LazyEntry(permission, lambda descriptor: Pair(give=PatternSet.all('T'))),))
  gift = Authority((LazyEntry(permission, lambda descriptor: Pair(PatternSet.all('T'))),))
  rules.gift(starter, gift, Descriptor('T'))
  with pytest.raises(Refusal, match='gift rule'):
    rules.gift(Authority(), gift, Descriptor('T'))


def test_gift_inclusion_respects_bounded_integer_domains():
  domain = Integers((IntegerInterval(1, 3),))
  rules = Rules({'T': Declaration({'N': Option('scalar', domain)})})
  permission = Permission('T')
  recipients = PatternSet((Pattern('T', {'N': domain}),))
  starter = Authority((Entry(permission, Pair(give=recipients)),))
  gift = Authority((Entry(permission, Pair(PatternSet.all('T'), PatternSet.all('T'))),))
  rules.gift(starter, gift, Descriptor('T'))


def test_lazy_results_cannot_hide_invalid_patterns_behind_a_cap(model):
  rules, _, _, _, _, _ = model
  invalid = Pair(PatternSet((Pattern('T', {}, frozenset({'N'})),)))
  authority = Authority((LazyEntry(Permission('T'), lambda descriptor: invalid),)).meet(
    Cap((Entry(Permission('T'), Pair()),))
  )
  with pytest.raises(Refusal, match='authority-neutral check'):
    rules.use(authority, Descriptor('T'))


def test_lazy_resolvers_receive_only_fixed_start_values(model):
  rules, _, permission, _, _, _ = model

  def resolve(descriptor):
    assert 'N' not in descriptor.values
    return Pair(give=PatternSet.all('T'))

  starter = Authority((LazyEntry(permission, resolve),))
  gift = Authority((Entry(permission, Pair(PatternSet.all('U'))),))
  rules.gift(starter, gift, Descriptor('T', {'N': 80}))


def test_joint_commit_checks_each_final_value_against_its_principal_by_oracle():
  domain = Finite(frozenset({'a', 'b'}), frozenset({'a', 'b'}))
  rules = Rules(
    {'T': Declaration({'A': Option('scalar', domain)}, defaults=lambda values, reading: {'A': 'a'})}
  )
  cases = descriptors('T', {'A': ['a', 'b']})
  top = Pair(PatternSet.all('T'))
  for starter_members, operator_members in product(powerset(['a', 'b']), repeat=2):
    starter = Authority(
      (
        Entry(Permission('T'), top),
        Entry(Permission('T', 'A', Finite(domain.base, starter_members)), top),
      )
    )
    operator = Authority((Entry(Permission('T', 'A', Finite(domain.base, operator_members)), top),))
    for requested, supplied in product(cases, repeat=2):
      try:
        pending = rules.prepare(Request('T', requested.values), starter, None)
      except Refusal:
        assert requested.values and requested.values['A'] not in starter_members
        continue
      expected = supplied.values.get('A', requested.values.get('A', 'a'))
      principal = operator if 'A' in supplied.values else starter
      allowed = evaluate(
        principal, Atom('T', 'A', expected), Descriptor('T'), Descriptor('T'), give=False
      )
      try:
        mission = rules.commit(pending, supplied.values, starter, operator)
      except Refusal:
        assert not allowed
      else:
        assert allowed
        assert mission.descriptor.values['A'] == expected


def test_resume_like_new_start_reapplies_defaults_under_the_new_reading(model):
  rules, h0, _, _, _, _ = model
  mission = rules.start(Request('T'), h0, 50)
  resumed = rules.start(mission.request, Authority.top(), 80)
  assert mission.descriptor.values['N'] == 50
  assert resumed.descriptor.values['N'] == 80
