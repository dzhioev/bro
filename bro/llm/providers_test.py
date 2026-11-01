import dataclasses
from datetime import date
from decimal import Decimal
from typing import Literal

import pytest

from bro.anthropic import declaration as anthropic
from bro.llm import providers
from bro.llm.llm import FAILURE_CATEGORIES, FailureSignature, Recipe
from bro.native import echo_declaration as echo
from bro.openai import declaration as openai_llm

ALL = providers.known_names()


def recipe_for(provider):
  recipe_class = providers.declaration(provider).Recipe
  return recipe_class(model=next(iter(recipe_class.MODEL_INPUTS)))


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
    assert providers.price('echo', 'echo', {}, None) == 0


@pytest.fixture
def broken_plugin(tmp_path, monkeypatch):
  import importlib.metadata

  package = tmp_path / 'faulty_provider'
  package.mkdir()
  (package / '__init__.py').write_text('')
  (package / 'credentials.py').write_text("KIND = {'description': 'fixture key'}\n")
  (package / 'declaration.py').write_text("raise RuntimeError('fixture declaration failed')\n")
  (package / 'persona.py').write_text('from faulty_provider.declaration import Recipe\n')
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

  assert 'faulty' not in dict(providers.declarations())
  assert 'faulty' not in BRO.llm_providers
  assert usage.model_family('arbitrary-2026-01-01') == 'arbitrary'
  assert Recipe.from_dict(echo.Recipe(model='echo').dump()) == echo.Recipe(model='echo')
  with pytest.raises(providers.RecipeError, match='faulty.*fixture declaration failed'):
    providers.bind_recipe('faulty:model')
  with pytest.raises(providers.RecipeError, match='faulty.*fixture declaration failed'):
    Recipe.from_dict({'type': 'faulty'})
  with pytest.raises(providers.RecipeError, match='faulty.*fixture declaration failed'):
    providers.bind_recipe('faulty:model')


@pytest.mark.usefixtures('broken_plugin')
def test_installation_preserves_broken_persona_and_provider_faults(monkeypatch):
  import importlib.metadata

  from bro import registry
  from ride.authority_reading import Configuration, Installation, Reading

  entries = (
    *registry._entry_points(),
    importlib.metadata.EntryPoint(
      name='faulty-persona',
      value='faulty_provider.persona:Persona',
      group='bro.bros.bro',
    ),
  )
  monkeypatch.setattr(registry, '_entry_points', lambda: entries)
  installation = Installation.take()
  assert 'fixture declaration failed' in installation.unavailable_bros['faulty-persona']
  assert 'fixture declaration failed' in installation.unavailable_providers['faulty']
  reading = Reading(Configuration.parse({}), {}, installation)
  from ride.authority_scope import resolve_recipe

  spec = resolve_recipe(reading, 'bro-dev', 'bro', ('echo:echo', '--llm')).recipe
  assert spec == echo.Recipe(model='echo')
  from bro.authority.rules import Request

  launch = reading.rules.start(
    Request('bro', {'target': 'bro-dev', 'harness': 'bro', 'provider': 'echo', 'model': 'echo'}),
    reading.operator(),
    reading,
  )
  assert launch.descriptor.values['provider'] == echo.Recipe.TYPE
  with pytest.raises(ValueError, match='faulty-persona.*unavailable.*fixture declaration failed'):
    resolve_recipe(reading, 'faulty-persona', 'bro', ('echo:echo', '--llm'))
  with pytest.raises(ValueError, match='faulty.*unavailable.*fixture declaration failed'):
    resolve_recipe(reading, 'bro-dev', 'bro', ('faulty:model', '--llm'))


def test_stored_recipe_loads_only_its_named_declaration(monkeypatch):
  loaded = []
  original = providers.declaration

  def load(name):
    loaded.append(name)
    return original(name)

  monkeypatch.setattr(providers, 'declaration', load)
  assert Recipe.from_dict(echo.Recipe(model='echo').dump()) == echo.Recipe(model='echo')
  assert loaded == [echo.Recipe.TYPE]


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
  with pytest.raises(providers.RecipeError, match='different.*TYPE'):
    providers.declaration('different')


def test_uninstalled_provider_refusal_names_installation_route():
  with pytest.raises(
    providers.RecipeError, match='unknown provider.*installed providers:.*install the plug-in'
  ):
    Recipe.from_dict({'type': 'absent-provider'})


@pytest.mark.parametrize('name', ALL)
def test_model_inputs_gate_each_declared_model_and_unlisted_models(name):
  recipe = recipe_for(name)
  for model, inputs in recipe.MODEL_INPUTS.items():
    selected = dataclasses.replace(recipe, model=model)
    for mime_type in inputs:
      assert selected.accepts(mime_type)
    assert not selected.accepts('application/x-unlisted-test')
  unlisted = dataclasses.replace(recipe, model=name + '-unlisted')
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
  ],
)
def test_malformed_model_inputs_make_the_declaration_unavailable(monkeypatch, table, error):
  monkeypatch.setattr(openai_llm.Recipe, 'MODEL_INPUTS', table)
  with pytest.raises(providers.RecipeError, match=error):
    providers.declaration('openai')


@pytest.mark.parametrize('provider', ALL)
@pytest.mark.parametrize('passthrough', [False, True])
def test_recipe_passthrough_round_trip_and_missing_key(provider, passthrough):
  from dataclasses import replace

  plain = recipe_for(provider)
  spec = replace(plain, passthrough=passthrough)
  dumped = spec.dump()
  assert ('passthrough' in dumped) is passthrough
  assert Recipe.from_dict(dumped) == spec
  assert Recipe.from_dict(plain.dump()).passthrough is False
  assert Recipe.from_dict({**plain.dump(), 'passthrough': False}) == plain


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_recipe_dump_requires_a_boolean_passthrough(value):
  with pytest.raises(TypeError, match='passthrough.*boolean'):
    Recipe.from_dict({**echo.Recipe(model='echo').dump(), 'passthrough': value})


@pytest.mark.parametrize(
  'text',
  [
    '',
    'openai',
    'openai:',
    ':sol',
    'openai:sol,',
    ' openai:sol',
    'openai:sol ',
    'openai:two words',
    'Openai:sol',
    'openai:sol,,effort=high',
    'openai:sol,effort=',
    'openai:sol,Effort=high',
    'openai:sol,effort=high low',
    'openai:sol+passthrough+passthrough',
    'openai:sol,reasoning_effort=high,reasoning_effort=low',
    'openai:sol+fast',
    'openai:sol+',
    'openai:sol,effort=high+passthrough,',
  ],
)
def test_recipe_grammar_refusals_name_the_text(text):
  with pytest.raises(providers.RecipeError) as failure:
    providers.parse_recipe(text)
  assert repr(text) in str(failure.value)


def test_complete_recipe_parsing_needs_no_installation(monkeypatch):
  monkeypatch.setattr(providers, 'declaration', lambda *_: pytest.fail('loaded declaration'))
  monkeypatch.setattr(providers, 'known_names', lambda: pytest.fail('read installation'))
  parsed = providers.parse_recipe('future-provider:region:model+passthrough,setting=a=b+c')
  assert parsed.provider == 'future-provider'
  assert parsed.model == 'region:model'
  assert parsed.flags == {'passthrough'}
  assert parsed.fields == {'setting': 'a=b+c'}


@pytest.mark.parametrize(
  'text,canonical',
  [
    ('openai:gpt-5.6-terra', 'openai:gpt-5.6-terra'),
    (
      'openai:gpt-5.6-terra,service_tier=priority,reasoning_effort=high',
      'openai:gpt-5.6-terra,reasoning_effort=high,service_tier=priority',
    ),
    ('anthropic:claude-opus-5-5,fast_mode=false', 'anthropic:claude-opus-5-5'),
    (
      'openai:gpt-7+passthrough,compact_threshold=00020',
      'openai:gpt-7+passthrough,compact_threshold=20',
    ),
  ],
)
def test_recipe_text_binds_and_canonicalizes(text, canonical):
  recipe = providers.bind_recipe(text)
  assert providers.format_recipe(recipe) == canonical
  assert providers.bind_recipe(canonical) == recipe


@pytest.mark.parametrize(
  'text,part',
  [
    ('anthropic:claude-opus-5-5,fast_mode=yes', 'fast_mode'),
    ('anthropic:claude-opus-5-5,fast_mode=1', 'fast_mode'),
    ('openai:gpt-5.6-terra,compact_threshold=1.5', 'compact_threshold'),
    ('openai:gpt-5.6-terra,compact_threshold=+1', 'compact_threshold'),
    ('openai:gpt-5.6-terra,compact_threshold=-1', 'compact_threshold'),
    ('openai:gpt-5.6-terra,reasoning_effort=None', 'reasoning_effort'),
    ('openai:gpt-5.6-terra,unknown=high', 'unknown'),
    ('openai:gpt-5.6-terra,passthrough=true', 'passthrough'),
    ('openai:gpt-5.6-terra,model=other', 'model'),
    ('deepseek:deepseek-v4-pro,reasoning_effort=medium', 'reasoning_effort'),
    ('missing-provider:model', 'installed providers'),
  ],
)
def test_recipe_binding_refusals_name_the_text_provider_and_part(text, part):
  with pytest.raises(providers.RecipeError) as failure:
    providers.bind_recipe(text)
  message = str(failure.value)
  assert repr(text) in message
  assert text.split(':')[0] in message
  assert part in message


@pytest.mark.parametrize('name', ALL)
def test_canonical_round_trip_across_each_provider_model_flag_and_field_value(name):
  import itertools
  from dataclasses import fields
  from typing import Literal, get_args, get_origin

  from bro.llm.llm import field_type, field_types

  recipe_class = providers.declaration(name).Recipe
  choices = {}
  for item in fields(recipe_class):
    if item.name == 'model':
      choices[item.name] = list(recipe_class.MODEL_INPUTS)
    elif item.name == 'passthrough':
      choices[item.name] = [False, True]
    else:
      annotation, nullable = field_type(field_types(recipe_class)[item.name])
      values = (
        list(get_args(annotation))
        if get_origin(annotation) is Literal
        else [False, True]
        if annotation is bool
        else [12345]
        if annotation is int
        else ['arbitrary:+text=value']
      )
      choices[item.name] = [None, *values] if nullable else values
  for values in itertools.product(*choices.values()):
    recipe = recipe_class(**dict(zip(choices, values, strict=True)))
    assert providers.bind_recipe(providers.format_recipe(recipe)) == recipe
    assert Recipe.from_dict(recipe.dump()) == recipe


@pytest.mark.parametrize('name', ALL)
def test_retired_records_stay_readable_and_writable_but_binding_names_the_retirement(name):
  module = providers.declaration(name)
  for model, retired_on in module.Recipe.RETIRED_MODELS.items():
    recipe = Recipe.from_dict({'type': name, 'model': model})
    text = providers.format_recipe(recipe)
    assert text == f'{name}:{model}'
    with pytest.raises(providers.RecipeError) as failure:
      providers.bind_recipe(text)
    message = str(failure.value)
    assert f'{model} was retired on {retired_on.isoformat()}' in message
    assert f'listed {name} models: ' + ', '.join(module.Recipe.MODEL_INPUTS) in message
    assert 'add +passthrough to run it anyway' in message
    assert providers.bind_recipe(text + '+passthrough').admitted()


def test_record_loading_uses_defaults_and_refuses_unknown_and_missing_keys():
  recipe = Recipe.from_dict({'type': 'openai', 'model': 'gpt-5.6-terra'})
  assert recipe.request_options() == {}
  assert recipe.dump()['compact_threshold'] is None
  with pytest.raises(ValueError, match='unknown.*unexpected'):
    Recipe.from_dict({**recipe.dump(), 'unexpected': 1})
  with pytest.raises(ValueError, match='requires model'):
    Recipe.from_dict({'type': 'openai'})
  with pytest.raises(KeyError, match='type'):
    Recipe.from_dict({'model': 'gpt-5.6-terra'})


@pytest.mark.parametrize(
  'retired,part',
  [
    ([], 'RETIRED_MODELS'),
    ({'old-model': '2026-11-01'}, 'old-model'),
    ({'echo': date(2026, 11, 1)}, 'echo.*both listed and retired'),
    ({'old-model': date(2026, 11, 1)}, 'old-model'),
  ],
)
def test_declaration_refuses_invalid_retirements_and_missing_price_rows(monkeypatch, retired, part):
  monkeypatch.setattr(echo.Recipe, 'RETIRED_MODELS', retired)
  with pytest.raises(providers.RecipeError, match=part):
    providers.declaration('echo')


def test_declaration_refuses_unclassified_price_rows(monkeypatch):
  from dataclasses import replace

  monkeypatch.setattr(echo, 'PRICE_TABLE', replace(echo.PRICE_TABLE, models={'extra-model': 0}))
  with pytest.raises(providers.RecipeError, match='extra-model'):
    providers.declaration('echo')


@pytest.mark.parametrize(
  'name,annotation,default',
  [
    ('knob', float, 1.0),
    ('knob', str | None, 'set'),
    ('knob', str, None),
    ('knob', int, True),
    ('knob', Literal['two words'], 'two words'),
    ('knob', Literal[1, 2], 1),
    ('knob', str | int, 'value'),
    ('type', str, 'reserved'),
    ('Bad_name', str, 'value'),
    ('passthrough', bool, False),
  ],
)
def test_declaration_refuses_invalid_field_schemas(monkeypatch, name, annotation, default):
  from dataclasses import make_dataclass

  recipe_class = make_dataclass(
    'InvalidRecipe', [(name, annotation, default)], bases=(echo.Recipe,), frozen=True
  )
  monkeypatch.setattr(echo, 'Recipe', recipe_class)
  with pytest.raises(providers.RecipeError, match=name):
    providers.declaration('echo')


def test_generic_fields_use_their_declared_types_and_validate_unwritable_values(monkeypatch):
  from dataclasses import dataclass

  @dataclass(frozen=True)
  class TypedRecipe(echo.Recipe):
    label: str = 'default'
    count: int = 0
    enabled: bool | None = None

  recipe_class = TypedRecipe
  monkeypatch.setattr(echo, 'Recipe', recipe_class)
  text = 'echo:echo,label=null:+value,count=-20,enabled=false'
  recipe = providers.bind_recipe(text)
  assert isinstance(recipe, TypedRecipe)
  assert recipe.label == 'null:+value'
  assert recipe.count == -20
  assert recipe.enabled is False
  assert providers.format_recipe(recipe) == text
  for values in (
    {'label': ''},
    {'label': 'two words'},
    {'label': 'a,b'},
    {'model': ''},
    {'model': 'two words'},
    {'model': 'a+b'},
    {'model': 'a,b'},
  ):
    with pytest.raises(ValueError, match='recipe text cannot write'):
      Recipe.from_dict({'type': 'echo', 'model': 'echo', **values})
  for values in ({'count': True}, {'enabled': 1}, {'label': 1}, {'passthrough': 1}):
    with pytest.raises(TypeError):
      Recipe.from_dict({'type': 'echo', 'model': 'echo', **values})


def test_harness_checks_recipe_support_without_resolving_or_switching_it():
  from bro.claude.harness import CLAUDE
  from bro.native.harness import BRO

  BRO.check_recipe(echo.Recipe(model='echo'))
  CLAUDE.check_recipe(anthropic.Recipe(model='claude-opus-5-5'))
  with pytest.raises(providers.RecipeError, match='bro.*cannot run.*anthropic.*claude harness'):
    BRO.check_recipe(anthropic.Recipe(model='claude-opus-5-5'))


def test_provider_discovery_does_not_construct_default_recipes(monkeypatch):
  from bro.claude.harness import CLAUDE
  from bro.native.harness import BRO

  def refuse(*args, **kwargs):
    pytest.fail('constructed a recipe during provider discovery')

  monkeypatch.setattr(echo.Recipe, '__init__', refuse)
  monkeypatch.setattr(anthropic.Recipe, '__init__', refuse)
  assert echo.Recipe.TYPE in BRO.llm_providers
  assert anthropic.Recipe.TYPE in CLAUDE.llm_providers


@pytest.mark.parametrize('text', ['openai:sol', 'deepseek:pro'])
def test_complete_recipe_admission_suggests_bindable_full_model_ids(text):
  provider = providers.parse_recipe(text).provider
  listed = providers.declaration(provider).Recipe.MODEL_INPUTS
  with pytest.raises(providers.RecipeError) as failure:
    providers.bind_recipe(text)
  assert f'choose one of: {", ".join(listed)};' in str(failure.value)
  for model in listed:
    assert providers.bind_recipe(f'{provider}:{model}').admitted()


@pytest.mark.parametrize(
  'annotation,model_field',
  [
    (str, dataclasses.field(default='echo')),
    (str, dataclasses.field(default_factory=lambda: 'echo')),
    (str | None, dataclasses.field()),
  ],
)
def test_declaration_requires_a_nonnullable_model_argument(monkeypatch, annotation, model_field):
  invalid = dataclasses.make_dataclass(
    'ImplicitModel',
    [('model', annotation, model_field)],
    bases=(echo.Recipe,),
    frozen=True,
  )
  monkeypatch.setattr(echo, 'Recipe', invalid)
  with pytest.raises(providers.RecipeError, match='model must be a required str'):
    providers.declaration('echo')
