from decimal import Decimal

import pytest

from bro.anthropic import declaration as anthropic
from bro.llm import providers
from bro.llm.llm import FAILURE_CATEGORIES, FailureSignature, LLMSpec
from bro.native import echo_declaration as echo
from bro.openai import declaration as openai_llm

ALL = providers.known_names()


@pytest.fixture
def shared_short_name(monkeypatch):
  tables = {name: dict(providers.models(name)) for name in ALL}
  tables['openai']['shared'] = 'gpt-shared'
  tables['anthropic']['shared'] = 'claude-shared'
  for name, table in tables.items():
    declaration = providers.declaration(name)
    monkeypatch.setattr(declaration, 'MODELS', table)
    monkeypatch.setattr(
      declaration.LLMSpec,
      'MODEL_INPUTS',
      {**declaration.LLMSpec.MODEL_INPUTS, **dict.fromkeys(table.values(), frozenset())},
    )


class TestRoster:
  def test_every_provider_declares_the_roster_contract(self):
    for name in providers.known_names():
      assert isinstance(providers.default_spec(name), providers.LLMSpec)
      assert isinstance(providers.models(name), dict)

  def test_a_provider_name_is_its_specs_type(self):
    for name in providers.known_names():
      assert providers.default_spec(name).TYPE == name

  def test_unknown_provider_names_the_known_ones(self):
    with pytest.raises(providers.LLMSelectionError, match='openai'):
      providers.default_spec('nope')


class TestModelResolution:
  def test_short_name_resolves_within_its_provider(self):
    assert providers.resolve_model('openai', 'sol') == openai_llm.MODELS['sol']
    assert providers.resolve_model('anthropic', 'fable5') == 'claude-fable-5'

  def test_an_unlisted_model_passes_through(self):
    assert providers.resolve_model('openai', 'gpt-6-unreleased') == 'gpt-6-unreleased'
    assert providers.resolve_model('anthropic', 'claude-opus-6') == 'claude-opus-6'

  @pytest.mark.parametrize('alias', ['opus', 'fable'])
  def test_a_name_off_the_providers_id_shape_is_refused(self, alias):
    with pytest.raises(providers.LLMSelectionError, match=f"'{alias}' names no anthropic model id"):
      providers.resolve_model('anthropic', alias)

  def test_every_declared_id_has_its_providers_shape(self):
    for name in ALL:
      for model in (providers.default_spec(name).dump()['model'], *providers.models(name).values()):
        assert providers.resolve_model(name, model) == model

  def test_provider_inferred_from_a_short_name(self):
    assert providers.provider_of_model('terra', ALL) == 'openai'
    assert providers.provider_of_model('opus5', ALL) == 'anthropic'

  def test_provider_inferred_from_a_full_id(self):
    assert providers.provider_of_model('claude-opus-5', ALL) == 'anthropic'

  def test_an_unknown_model_lists_the_short_names(self):
    with pytest.raises(providers.LLMSelectionError, match='name its provider'):
      providers.provider_of_model('gpt-6-unreleased', ALL)

  def test_a_model_resolves_only_among_the_given_providers(self):
    with pytest.raises(providers.LLMSelectionError, match="unknown model 'opus5'"):
      providers.provider_of_model('opus5', ('openai', 'echo'))

  @pytest.mark.usefixtures('shared_short_name')
  def test_a_name_two_of_the_given_providers_carry_is_ambiguous(self):
    assert providers.provider_of_model('shared', ('anthropic',)) == 'anthropic'
    with pytest.raises(providers.LLMSelectionError, match='more than one provider'):
      providers.provider_of_model('shared', ALL)


class TestParsing:
  @pytest.mark.parametrize(
    'value,expected',
    [
      ('openai:sol:max+fast', providers.LLMSelection('openai', 'sol', 'max', True)),
      ('::high', providers.LLMSelection(effort='high')),
      (':fable5', providers.LLMSelection(model='fable5')),
      ('anthropic::high', providers.LLMSelection(provider='anthropic', effort='high')),
      ('openai', providers.LLMSelection(provider='openai')),
      ('+fast', providers.LLMSelection(fast=True)),
      (':fable5:max', providers.LLMSelection(model='fable5', effort='max')),
    ],
  )
  def test_round_trips_through_the_canonical_form(self, value, expected):
    parsed = providers.parse(value)
    assert parsed == expected
    assert parsed.format() == value

  @pytest.mark.parametrize(
    'value',
    [
      '',
      '::ludicrous',
      'a:b:c:d',
      'openai+slow',
      '+fast+fast',
      'Bad Provider',
      ':model name',
    ],
  )
  def test_malformed_values_are_rejected(self, value):
    with pytest.raises(providers.LLMSelectionError):
      providers.parse(value)

  @pytest.mark.usefixtures('shared_short_name')
  def test_a_model_several_providers_carry_parses(self):
    assert providers.parse(':shared') == providers.LLMSelection(model='shared')


class TestSelectionProviders:
  def test_a_named_provider_is_its_own_when_among_them(self):
    selection = providers.parse('anthropic::high')
    assert selection.providers(ALL) == ('anthropic',)
    assert selection.providers(('openai', 'echo')) == ()

  def test_a_model_named_alone_runs_on_the_providers_carrying_it(self):
    assert providers.parse(':opus55').providers(ALL) == ('anthropic',)
    assert providers.parse(':opus55').providers(('openai', 'echo')) == ()

  def test_a_selection_naming_neither_runs_on_every_one(self):
    assert providers.parse('::high').providers(('openai', 'echo')) == ('openai', 'echo')


class TestOver:
  def test_an_unnamed_slot_reads_the_base(self):
    flags = providers.LLMSelection(effort='low')
    base = providers.LLMSelection('openai', 'sol', 'xhigh')
    assert flags.over(base) == providers.LLMSelection('openai', 'sol', 'low')

  def test_a_provider_named_alone_keeps_its_default_model(self):
    flags = providers.LLMSelection(provider='openai')
    base = providers.LLMSelection(model='fable5', effort='high')
    assert flags.over(base) == flags

  def test_a_model_named_alone_inherits_the_base_provider(self):
    flags = providers.LLMSelection(model='terra')
    base = providers.LLMSelection('openai', 'sol', 'xhigh')
    assert flags.over(base) == providers.LLMSelection('openai', 'terra', 'xhigh')

  def test_fast_holds_from_either_side(self):
    assert providers.LLMSelection(fast=True).over(providers.LLMSelection()).fast
    assert providers.LLMSelection().over(providers.LLMSelection(fast=True)).fast

  def test_an_empty_selection_is_the_base(self):
    base = providers.LLMSelection('openai', 'sol', 'xhigh', True)
    assert providers.LLMSelection().over(base) == base


class TestResolve:
  def test_an_empty_selection_leaves_the_base(self):
    base = openai_llm.LLMSpec(model='gpt-6.1-sol', reasoning_effort='high')
    assert providers.resolve(base, providers.LLMSelection(), ALL) == base

  def test_a_model_alone_keeps_the_bases_other_knobs(self):
    base = openai_llm.LLMSpec(model='gpt-6.1-sol', reasoning_effort='high')
    resolved = providers.resolve(base, providers.parse(':terra'), ALL)
    assert resolved == openai_llm.LLMSpec(model='gpt-5.6-terra', reasoning_effort='high')

  def test_a_provider_selects_its_own_default_recipe(self):
    base = openai_llm.LLMSpec(model='gpt-6.1-sol', reasoning_effort='high')
    resolved = providers.resolve(base, providers.parse('openai'), ALL)
    assert resolved == openai_llm.LLMSpec()

  def test_a_model_alone_is_resolved_within_the_base_provider(self):
    base = anthropic.LLMSpec()
    with pytest.raises(providers.LLMSelectionError, match='names no anthropic model id'):
      providers.resolve(base, providers.parse(':terra'), ALL)

  @pytest.mark.parametrize('value', ['', ':terra', '::high', '+fast'])
  def test_only_a_named_provider_supplies_an_absent_base(self, value):
    selection = providers.parse(value) if value else providers.LLMSelection()
    with pytest.raises(
      providers.LLMSelectionError, match='no LLM recipe: pass --llm provider:model'
    ):
      providers.resolve(None, selection, ALL)
    assert providers.resolve(None, providers.parse('openai:terra:high'), ALL) == openai_llm.LLMSpec(
      model=openai_llm.MODELS['terra'], reasoning_effort='high'
    )

  def test_knobs_apply_over_whichever_recipe_was_selected(self):
    resolved = providers.resolve(
      openai_llm.LLMSpec(), providers.parse('anthropic:sonnet5:max+fast'), ALL
    )
    assert resolved == anthropic.LLMSpec(model='claude-sonnet-5', effort='max', fast_mode=True)

  def test_fast_falls_back_on_a_provider_without_it(self):
    assert providers.resolve(echo.LLMSpec(), providers.parse('+fast'), ALL) == echo.LLMSpec()

  def test_an_effort_override_raises_on_a_provider_without_it(self):
    with pytest.raises(NotImplementedError, match='effort override'):
      providers.resolve(echo.LLMSpec(), providers.parse('::high'), ALL)


class TestNativeSplit:
  def test_anthropic_is_a_recipe_the_native_loop_cannot_run(self):
    from bro.llm import wires

    assert anthropic.LLMSpec.WIRE not in wires.client_names()

  def test_the_api_providers_build_clients(self):
    assert isinstance(openai_llm.LLMSpec(), LLMSpec)
    assert isinstance(echo.LLMSpec(), LLMSpec)

  def test_every_provider_round_trips_through_from_dict(self):
    for name in providers.known_names():
      spec = providers.default_spec(name)
      assert providers.LLMSpec.from_dict(spec.dump()) == spec


class TestFailureSignatures:
  def test_every_provider_declares_its_signatures(self):
    for name in providers.known_names():
      for signature in providers.failure_signatures(name):
        assert signature.category in FAILURE_CATEGORIES

  def test_signatures_reach_the_providers_declaration(self):
    assert providers.failure_signatures('openai') == openai_llm.FAILURE_SIGNATURES

  def test_a_signature_off_the_category_vocabulary_is_refused(self):
    with pytest.raises(ValueError, match='unknown failure category'):
      FailureSignature(pattern='x', category='sprint')


class TestPricing:
  def test_pricing_dispatches_to_the_named_provider(self):
    assert providers.price(
      'openai',
      'gpt-5.6-terra',
      {'input_tokens': 1, 'output_tokens': 1},
      'default',
    ) == Decimal('0.000014')

  def test_price_tables_reach_the_provider_declaration(self):
    assert providers.price_table('openai') is openai_llm.PRICE_TABLE

  def test_echo_has_no_billable_calls(self):
    assert providers.price('echo', 'echo', {}, None) is None


def test_recipe_selection_round_trip_preserves_provider_parameters():
  from bro.anthropic.declaration import LLMSpec as AnthropicSpec
  from bro.native.echo_declaration import LLMSpec as EchoSpec
  from bro.openai.declaration import LLMSpec as OpenAISpec

  for spec in (
    AnthropicSpec(model=None, effort=None),
    EchoSpec(),
    OpenAISpec(reasoning_effort='minimal', service_tier='flex', compact_threshold=12345),
  ):
    assert spec.with_selection(spec.selection()) == spec
  for value in ('none', 'minimal', None):
    selected = OpenAISpec().with_selection({'model': 'opaque-model', 'effort': value})
    assert selected.model == 'opaque-model'
    assert selected.reasoning_effort == value
  with pytest.raises(ValueError, match='cannot defer model'):
    OpenAISpec().with_selection({'model': None, 'effort': None})
  with pytest.raises(ValueError, match='selection fields'):
    EchoSpec().with_selection({'model': 'echo', 'effort': None})


@pytest.fixture
def broken_plugin(tmp_path, monkeypatch):
  import importlib.metadata

  package = tmp_path / 'faulty_provider'
  package.mkdir()
  (package / '__init__.py').write_text('')
  (package / 'credentials.py').write_text("KIND = {'description': 'fixture key'}\n")
  (package / 'declaration.py').write_text("raise RuntimeError('fixture declaration failed')\n")
  (package / 'persona.py').write_text('from faulty_provider.declaration import LLMSpec\n')
  monkeypatch.syspath_prepend(str(tmp_path))
  entries = (
    *providers._entry_points(),
    importlib.metadata.EntryPoint(
      name='faulty',
      value='faulty_provider.declaration',
      group=providers.ENTRY_POINT_GROUP,
    ),
  )
  monkeypatch.setattr(providers, '_entry_points', lambda: entries)
  return package


def test_known_names_reads_metadata_without_loading_entries(monkeypatch):
  import importlib.metadata

  entries = (
    importlib.metadata.EntryPoint(
      name='zeta', value='missing_zeta', group=providers.ENTRY_POINT_GROUP
    ),
    importlib.metadata.EntryPoint(
      name='alpha', value='missing_alpha', group=providers.ENTRY_POINT_GROUP
    ),
  )
  monkeypatch.setattr(providers, '_entry_points', lambda: entries)
  monkeypatch.setattr(
    importlib.metadata.EntryPoint, 'load', lambda self: pytest.fail('loaded a declaration')
  )
  assert providers.known_names() == ('alpha', 'zeta')


@pytest.mark.usefixtures('broken_plugin')
def test_broken_declaration_is_skipped_by_searches_but_refused_by_name():
  from bro.llm import usage
  from bro.native.harness import BRO

  assert providers.provider_of_model('terra', providers.known_names()) == openai_llm.LLMSpec.TYPE
  assert 'faulty' not in dict(providers.available_specs())
  assert 'faulty' not in BRO.llm_providers
  assert openai_llm.LLMSpec().effort_values <= providers.effort_values()
  assert usage.model_family('arbitrary-2026-01-01') == 'arbitrary'
  assert LLMSpec.from_dict(echo.LLMSpec().dump()) == echo.LLMSpec()
  with pytest.raises(providers.LLMSelectionError, match='faulty.*fixture declaration failed'):
    providers.default_spec('faulty')
  with pytest.raises(providers.LLMSelectionError, match='faulty.*fixture declaration failed'):
    LLMSpec.from_dict({'type': 'faulty'})
  with pytest.raises(providers.LLMSelectionError, match='faulty.*fixture declaration failed'):
    BRO.resolve_recipe(echo.LLMSpec(), providers.parse('faulty'))


@pytest.mark.usefixtures('broken_plugin')
def test_installation_preserves_broken_persona_and_provider_faults(monkeypatch):
  import importlib.metadata

  from bro import registry
  from ride.authority_reading import Configuration, Installation, Reading
  from ride.authority_scope import recipe

  entries = (
    *registry._entry_points(),
    importlib.metadata.EntryPoint(
      name='faulty-persona',
      value='faulty_provider.persona:Persona',
      group='bro',
    ),
  )
  monkeypatch.setattr(registry, '_entry_points', lambda: entries)
  installation = Installation.take()
  assert 'fixture declaration failed' in installation.unavailable_bros['faulty-persona']
  assert 'fixture declaration failed' in installation.unavailable_providers['faulty']
  reading = Reading(Configuration.parse({}), {}, installation)
  spec = recipe({'target': 'bro-dev', 'harness': 'bro', 'provider': 'echo'}, reading)
  assert spec == echo.LLMSpec()
  from bro.authority.rules import Request

  launch = reading.rules.start(
    Request('bro', {'target': 'bro-dev', 'harness': 'bro', 'provider': 'echo'}),
    reading.operator(),
    reading,
  )
  assert launch.descriptor.values['provider'] == echo.LLMSpec.TYPE
  with pytest.raises(ValueError, match='faulty-persona.*unavailable.*fixture declaration failed'):
    recipe({'target': 'faulty-persona', 'harness': 'bro'}, reading)
  with pytest.raises(ValueError, match='faulty.*unavailable.*fixture declaration failed'):
    recipe({'target': 'bro-dev', 'harness': 'bro', 'provider': 'faulty'}, reading)


def test_stored_recipe_loads_only_its_named_declaration(monkeypatch):
  loaded = []
  original = providers.declaration

  def load(name):
    loaded.append(name)
    return original(name)

  monkeypatch.setattr(providers, 'declaration', load)
  assert LLMSpec.from_dict(echo.LLMSpec().dump()) == echo.LLMSpec()
  assert loaded == [echo.LLMSpec.TYPE]


@pytest.mark.parametrize('names', [('same', 'same'), ('Invalid',)])
def test_invalid_or_duplicate_entry_names_are_refused(names, monkeypatch):
  import importlib.metadata

  entries = tuple(
    importlib.metadata.EntryPoint(name=name, value='missing', group=providers.ENTRY_POINT_GROUP)
    for name in names
  )
  monkeypatch.setattr(providers, '_entry_points', lambda: entries)
  with pytest.raises(ValueError, match='duplicate|invalid'):
    providers.known_names()


def test_declaration_type_must_match_entry_name(monkeypatch):
  import importlib.metadata

  entry = importlib.metadata.EntryPoint(
    name='different', value='bro.native.echo_declaration', group=providers.ENTRY_POINT_GROUP
  )
  monkeypatch.setattr(providers, '_entry_points', lambda: (entry,))
  with pytest.raises(providers.LLMSelectionError, match='different.*TYPE'):
    providers.default_spec('different')


def test_uninstalled_provider_refusal_names_installation_route():
  with pytest.raises(
    providers.LLMSelectionError, match='unknown provider.*installed providers:.*install the plug-in'
  ):
    LLMSpec.from_dict({'type': 'absent-provider'})


def test_native_refusal_identifies_a_decode_only_wire():
  from bro.native.harness import BRO

  with pytest.raises(providers.LLMSelectionError, match="wire 'messages'"):
    BRO.resolve_recipe(echo.LLMSpec(), providers.parse('anthropic'))


def test_selection_parsing_does_not_consult_the_installation(monkeypatch):
  monkeypatch.setattr(providers, 'known_names', lambda: pytest.fail('read installation'))
  monkeypatch.setattr(providers, 'declaration', lambda *_: pytest.fail('loaded declaration'))
  assert providers.parse('future-provider:future-model:high').provider == 'future-provider'
  assert providers.parse(':future-model').model == 'future-model'
  assert providers.LLMSelection(provider='future-provider').format() == 'future-provider'


@pytest.mark.parametrize('name', ALL)
def test_model_inputs_gate_each_declared_model_and_unlisted_models(name):
  declaration = providers.declaration(name)
  recipe = declaration.LLMSpec()
  for model, inputs in recipe.MODEL_INPUTS.items():
    selected = recipe.with_selection({**recipe.selection(), 'model': model})
    for mime_type in inputs:
      assert selected.accepts(mime_type)
    assert not selected.accepts('application/x-unlisted-test')
  unlisted = recipe.with_selection({**recipe.selection(), 'model': name + '-unlisted'})
  assert not unlisted.accepts('image/png')


@pytest.mark.parametrize(
  'table,error',
  [
    ([], 'map model ids'),
    ({1: frozenset()}, 'map model ids'),
    ({'model': {'application/pdf'}}, 'map model ids'),
    ({'model': frozenset({1})}, 'map model ids'),
    ({'model': frozenset({'Application/PDF'})}, 'map model ids'),
    ({'model': frozenset({'text/plain; charset=utf-8'})}, 'map model ids'),
    ({}, 'cover DEFAULT_MODEL'),
  ],
)
def test_malformed_model_inputs_make_the_declaration_unavailable(monkeypatch, table, error):
  monkeypatch.setattr(openai_llm.LLMSpec, 'MODEL_INPUTS', table)
  with pytest.raises(providers.LLMSelectionError, match=error):
    providers.declaration('openai')


@pytest.mark.parametrize('provider', ALL)
@pytest.mark.parametrize('passthrough', [False, True])
def test_recipe_passthrough_round_trip_and_missing_key(provider, passthrough):
  from dataclasses import replace

  plain = providers.default_spec(provider)
  spec = replace(plain, passthrough=passthrough)
  dumped = spec.dump()
  assert ('passthrough' in dumped) is passthrough
  assert LLMSpec.from_dict(dumped) == spec
  assert LLMSpec.from_dict(plain.dump()).passthrough is False
  assert LLMSpec.from_dict({**plain.dump(), 'passthrough': False}) == plain


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_recipe_dump_requires_a_boolean_passthrough(value):
  with pytest.raises(TypeError, match='passthrough.*boolean'):
    LLMSpec.from_dict({**echo.LLMSpec().dump(), 'passthrough': value})


@pytest.mark.parametrize('suffix', ['+passthrough', '+fast+passthrough', '+passthrough+fast'])
def test_passthrough_suffixes_canonicalize_and_admit_after_resolution(suffix):
  selection = providers.parse('openai:gpt-7:high' + suffix)
  assert selection.passthrough
  assert (
    selection.format() == 'openai:gpt-7:high' + ('+fast' if selection.fast else '') + '+passthrough'
  )
  recipe = providers.resolve(None, selection, ALL)
  providers.require_admitted(recipe)
  assert recipe.accepts('image/tiff')


@pytest.mark.parametrize('value', ['+passthrough+passthrough', '+passthrough+slow', '+fast+'])
def test_duplicate_or_unknown_suffixes_are_refused(value):
  with pytest.raises(providers.LLMSelectionError):
    providers.parse(value)


def test_passthrough_inherits_except_when_a_provider_is_named():
  from dataclasses import replace

  base = replace(openai_llm.LLMSpec(), passthrough=True)
  for text in (':terra', '::high', '+fast', '+passthrough'):
    selection = providers.parse(text)
    assert selection.over(providers.LLMSelection(passthrough=True)).passthrough
    assert providers.resolve(base, selection, ALL).passthrough
  assert not providers.parse('openai').over(providers.parse('+passthrough')).passthrough
  assert not providers.resolve(base, providers.parse('openai'), ALL).passthrough


def test_resolution_and_reading_preserve_an_unlisted_model_until_it_drives_a_run():
  recipe = providers.resolve(None, providers.parse('openai:gpt-7:high'), ALL)
  assert LLMSpec.from_dict(recipe.dump()) == recipe
  assert not recipe.admitted()
  with pytest.raises(providers.LLMSelectionError, match='gpt-7.*openai.*sol.*\\+passthrough'):
    providers.require_admitted(recipe)
  assert anthropic.LLMSpec(model=None).admitted()
  for provider in ALL:
    providers.require_admitted(providers.default_spec(provider))
