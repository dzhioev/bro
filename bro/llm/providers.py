"""Provider declarations, complete recipe text, admission and pricing."""

import dataclasses
import functools
import importlib.metadata
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType, ModuleType
from typing import Any, Literal, Optional, get_args, get_origin

from bro.base import log
from bro.llm.llm import (
  FailureSignature,
  Recipe,
  field_type,
  field_types,
  validate_field,
)
from bro.llm.wire import MIME_TYPE

ENTRY_POINT_GROUP = 'bro.llm.providers'


class RecipeError(ValueError):
  """Recipe text or a provider declaration cannot be bound."""


@functools.cache
def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def _entries() -> dict[str, importlib.metadata.EntryPoint]:
  entries: dict[str, importlib.metadata.EntryPoint] = {}
  for entry in _entry_points():
    if re.fullmatch(r'[a-z][a-z0-9-]*', entry.name) is None:
      raise ValueError(f'invalid provider name {entry.name!r}')
    if entry.name in entries:
      raise ValueError(f'duplicate provider {entry.name!r}')
    entries[entry.name] = entry
  return entries


def known_names() -> tuple[str, ...]:
  return tuple(sorted(_entries()))


def declaration(provider: str) -> ModuleType:
  entries = _entries()
  if provider not in entries:
    raise RecipeError(
      f'unknown provider {provider!r}; installed providers: {", ".join(entries) or "(none)"}; '
      'install the plug-in that provides it'
    )
  try:
    module = entries[provider].load()
    if not isinstance(module, ModuleType):
      raise TypeError('entry must load a declaration module')
    if not issubclass(module.Recipe, Recipe) or module.Recipe.TYPE != provider:
      raise ValueError('declaration Recipe.TYPE must equal the entry name')
    if not isinstance(module.Recipe.WIRE, str):
      raise TypeError('declaration must name a wire')
    inputs = module.Recipe.MODEL_INPUTS
    if not isinstance(inputs, Mapping) or any(
      not isinstance(model, str)
      or not isinstance(types, frozenset)
      or any(
        not isinstance(mime_type, str) or MIME_TYPE.fullmatch(mime_type) is None
        for mime_type in types
      )
      for model, types in inputs.items()
    ):
      raise TypeError(
        'declaration MODEL_INPUTS must map model ids to frozensets of lowercase MIME types'
      )
    _check_recipe_class(module.Recipe, module.PRICE_TABLE)
    if not isinstance(module.FAILURE_SIGNATURES, tuple) or any(
      not isinstance(signature, FailureSignature) for signature in module.FAILURE_SIGNATURES
    ):
      raise TypeError('declaration must export FAILURE_SIGNATURES')
    if not callable(module.price) or not isinstance(module.PRICE_TABLE_SHA256, str):
      raise TypeError('declaration must export pricing and its table digest')
    if not hasattr(module, 'PRICE_TABLE'):
      raise TypeError('declaration must export PRICE_TABLE')
    if hasattr(module, 'MODEL_SLUG') and not callable(module.model_family):
      raise TypeError('declaration MODEL_SLUG requires model_family')
  except Exception as error:
    raise RecipeError(f'provider {provider!r} is unavailable: {error}') from error
  return module


def declarations(among: Collection[str] | None = None) -> Iterator[tuple[str, ModuleType]]:
  for name in known_names():
    if among is not None and name not in among:
      continue
    try:
      module = declaration(name)
    except RecipeError as error:
      log.warning('%s', error)
      continue
    yield name, module


def failure_signatures(provider: str) -> tuple[FailureSignature, ...]:
  """the provider's declared failure signatures — how its client's failures
  read in a run's error output, each classified into `FAILURE_CATEGORIES`."""
  return declaration(provider).FAILURE_SIGNATURES


def price(
  provider: str,
  model: str,
  usage: Mapping[str, Any],
  service_tier: Optional[str],
  table: Any = None,
) -> Optional[Decimal]:
  """Price one call in its provider's raw usage vocabulary."""
  return declaration(provider).price(model, usage, service_tier, table)


def price_table(provider: str) -> Any:
  """The provider's current price table, including its provenance and rates."""
  return declaration(provider).PRICE_TABLE


def price_table_from_content(
  provider: str, source: str, as_of: str, models: Mapping[str, Any]
) -> Any:
  """Reconstruct one provider's price table from persisted vendor-vocabulary rates."""
  factory = getattr(declaration(provider), 'price_table_from_content', None)
  if not callable(factory):
    raise ValueError(f'provider {provider!r} has no persisted price-table vocabulary')
  return factory(source, as_of, models)


def require_admitted(recipe: Recipe) -> None:
  if not recipe.admitted():
    model = recipe.model
    if model in recipe.RETIRED_MODELS:
      raise RecipeError(
        f'{model} was retired on {recipe.RETIRED_MODELS[model].isoformat()}; '
        f'listed {recipe.TYPE} models: {", ".join(recipe.MODEL_INPUTS)}; '
        'add +passthrough to run it anyway'
      )
    raise RecipeError(
      f'model {model!r} is not listed by provider {recipe.TYPE!r}; '
      f'choose one of: {", ".join(recipe.MODEL_INPUTS)}; '
      'or add +passthrough to send the model and content unchecked'
    )


def _check_recipe_class(recipe_class: type[Recipe], price_table: Any) -> None:
  retired = recipe_class.RETIRED_MODELS
  if not isinstance(retired, Mapping):
    raise TypeError('RETIRED_MODELS must map model ids to retirement dates')
  for model, retired_on in retired.items():
    if type(model) is not str or re.fullmatch(r'[^+,\s]+', model) is None:
      raise ValueError(f'invalid retired model {model!r}')
    if type(retired_on) is not date:
      raise TypeError(f'retired model {model!r} requires a retirement date')
    if model in recipe_class.MODEL_INPUTS:
      raise ValueError(f'model {model!r} is both listed and retired')
  expected = recipe_class.MODEL_INPUTS.keys() | retired.keys()
  priced = price_table.models.keys()
  if expected != priced:
    raise ValueError(
      f'price table models must be exactly listed and retired models; '
      f'missing: {sorted(expected - priced)}; extra: {sorted(priced - expected)}'
    )
  if 'passthrough' in recipe_class.__annotations__:
    raise ValueError('provider must not declare passthrough')
  annotations = field_types(recipe_class)
  model_field = next(item for item in dataclasses.fields(recipe_class) if item.name == 'model')
  if (
    annotations['model'] is not str
    or model_field.default is not dataclasses.MISSING
    or model_field.default_factory is not dataclasses.MISSING
  ):
    raise TypeError('recipe model must be a required str')
  for item in dataclasses.fields(recipe_class):
    if item.name in {'model', 'passthrough'}:
      continue
    if item.name == 'type' or re.fullmatch(r'[a-z][a-z0-9_]*', item.name) is None:
      raise ValueError(f'invalid recipe field {item.name!r}')
    try:
      _, nullable = field_type(annotations[item.name])
      if item.default is dataclasses.MISSING or (nullable and item.default is not None):
        raise ValueError('field requires a default; optional fields require default None')
      validate_field(item.name, item.default, annotations[item.name])
    except (TypeError, ValueError) as error:
      raise ValueError(f'invalid recipe field {item.name!r}: {error}') from error


@dataclass(frozen=True)
class ParsedRecipe:
  provider: str
  model: str
  flags: frozenset[str]
  fields: Mapping[str, str]


def parse_recipe(text: str) -> ParsedRecipe:
  """Read recipe grammar without loading a declaration or installation metadata."""

  def refuse(reason: str):
    raise RecipeError(f'invalid recipe {text!r}: {reason}')

  body, *assignments = text.split(',')
  provider, separator, tail = body.partition(':')
  model, *flags = tail.split('+')
  if (
    not separator
    or re.fullmatch(r'[a-z][a-z0-9-]*', provider) is None
    or re.fullmatch(r'[^+,\s]+', model) is None
  ):
    refuse('expected provider:model[+passthrough][,field=value]...')
  if len(set(flags)) != len(flags):
    refuse('duplicate flag')
  if any(flag != 'passthrough' for flag in flags):
    refuse('the only framework flag is +passthrough')
  values = {}
  for assignment in assignments:
    name, separator, value = assignment.partition('=')
    if (
      not separator
      or re.fullmatch(r'[a-z][a-z0-9_]*', name) is None
      or re.fullmatch(r'[^,\s]+', value) is None
    ):
      refuse(f'invalid field assignment {assignment!r}')
    if name in values:
      refuse(f'duplicate field {name!r}')
    values[name] = value
  return ParsedRecipe(provider, model, frozenset(flags), MappingProxyType(values))


def _bind_value(value: str, annotation: Any) -> Any:
  annotation, _ = field_type(annotation)
  if annotation is bool:
    if value not in {'true', 'false'}:
      raise ValueError('expected true or false')
    return value == 'true'
  if annotation is int:
    if re.fullmatch(r'-?[0-9]+', value) is None:
      raise ValueError('expected a decimal integer')
    return int(value)
  if get_origin(annotation) is Literal and value not in get_args(annotation):
    raise ValueError(f'expected one of {get_args(annotation)}')
  return value


def bind_recipe(text: str) -> Recipe:
  parsed = parse_recipe(text)
  try:
    recipe_class = declaration(parsed.provider).Recipe
    annotations = field_types(recipe_class)
    provider_fields = annotations.keys() - {'model', 'passthrough'}
    unknown = parsed.fields.keys() - provider_fields
    if unknown:
      raise ValueError(
        f'unknown field {", ".join(sorted(unknown))}; '
        f'fields: {", ".join(name for name in annotations if name in provider_fields) or "(none)"}'
      )
    values = {}
    for name, value in parsed.fields.items():
      try:
        values[name] = _bind_value(value, annotations[name])
      except ValueError as error:
        raise ValueError(f'field {name!r}: {error}') from error
    recipe = recipe_class(model=parsed.model, passthrough='passthrough' in parsed.flags, **values)
    require_admitted(recipe)
    return recipe
  except (TypeError, ValueError) as error:
    raise RecipeError(f'recipe {text!r} for provider {parsed.provider!r}: {error}') from error


def format_recipe(recipe: Recipe) -> str:
  model = recipe.model
  text = f'{recipe.TYPE}:{model}'
  if recipe.passthrough:
    text += '+passthrough'
  for item in dataclasses.fields(recipe):
    if item.name in {'model', 'passthrough'}:
      continue
    value = getattr(recipe, item.name)
    if value != item.default:
      rendered = str(value).lower() if type(value) is bool else str(value)
      text += f',{item.name}={rendered}'
  return text
