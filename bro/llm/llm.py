import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date
from typing import Any, ClassVar, Literal, Union, get_args, get_origin, get_type_hints

from bro.llm.records import Stop

# the neutral vocabulary a provider classifies its failures into; a consumer
# (e.g. a retry policy) maps these categories onto its own exception taxonomy.
FAILURE_CATEGORIES = frozenset(
  {
    'rate-limit',
    'server-error',
    'network',
    'authentication',
    'model-not-found',
    'usage-limit',
    'unknown-api',
  }
)


@dataclass(frozen=True)
class FailureSignature:
  """one provider failure as it reads in a run's error output — the exception
  spelling the provider's own client emits: a regex over that output, and the
  `FAILURE_CATEGORIES` category a match indicates."""

  pattern: str
  category: str

  def __post_init__(self):
    if self.category not in FAILURE_CATEGORIES:
      raise ValueError(
        f'unknown failure category {self.category!r}; expected one of '
        f'{", ".join(sorted(FAILURE_CATEGORIES))}'
      )


class LLMUnavailable(RuntimeError):
  """The LLM provider kept failing transiently through a client's whole retry window."""


class LLMStopped(RuntimeError):
  """A response ended without completing the requested leg."""

  def __init__(self, reason: Stop, stop_reason: str | None = None):
    self.reason = reason
    self.stop_reason = stop_reason
    super().__init__(f'LLM stopped: {reason}' + (f' ({stop_reason})' if stop_reason else ''))


class UnsupportedContent(ValueError):
  """A recipe cannot accept an input content part."""


@dataclass(frozen=True)
class Recipe:
  """A model, provider-native fields and framework flags for one wire."""

  TYPE: ClassVar[str]
  MODEL_INPUTS: ClassVar[Mapping[str, frozenset[str]]]
  RETIRED_MODELS: ClassVar[Mapping[str, date]]
  WIRE: ClassVar[str]
  ENDPOINT: ClassVar[str | None] = None
  model: str
  passthrough: bool = field(default=False, kw_only=True)

  def __post_init__(self):
    for name, annotation in field_types(type(self)).items():
      value = getattr(self, name)
      validate_field(name, value, annotation)
      if name == 'model' and re.fullmatch(r'[^+,\s]+', value) is None:
        raise ValueError(f'invalid model {value!r}: recipe text cannot write it')

  def admitted(self) -> bool:
    model = self.model
    return self.passthrough or model in self.MODEL_INPUTS

  def request_options(self) -> dict:
    return {}

  def is_transient(self, error: Exception) -> bool:
    return False

  def accepts(self, mime_type: str) -> bool:
    model = self.model
    return self.passthrough or (
      model in self.MODEL_INPUTS and mime_type in self.MODEL_INPUTS[model]
    )

  @classmethod
  def needed_secrets(cls) -> tuple[str, ...]:
    """Credentials this provider needs on surfaces running it as an LLM process."""
    return ()

  def dump(self) -> dict:
    data = {'type': self.TYPE}
    data.update(
      (item.name, getattr(self, item.name))
      for item in fields(self)
      if item.name != 'passthrough' or self.passthrough
    )
    return data

  @classmethod
  def from_dict(cls, data: dict) -> 'Recipe':
    """Rebuild a record without checking whether its model is still admitted."""
    from bro.llm import providers

    recipe_class = providers.declaration(data['type']).Recipe
    if 'model' not in data:
      raise ValueError('recipe record requires model')
    unknown = data.keys() - {'type', *field_types(recipe_class)}
    if unknown:
      raise ValueError(f'unknown recipe fields: {", ".join(sorted(unknown))}')
    return recipe_class(**{name: value for name, value in data.items() if name != 'type'})


def field_types(recipe_class: type[Recipe]) -> dict[str, Any]:
  annotations = get_type_hints(recipe_class)
  return {item.name: annotations[item.name] for item in fields(recipe_class)}


def field_type(annotation: Any) -> tuple[Any, bool]:
  nullable = get_origin(annotation) in (Union, types.UnionType)
  if nullable:
    arguments = get_args(annotation)
    if len(arguments) != 2 or type(None) not in arguments:
      raise TypeError(f'unsupported recipe field type {annotation}')
    annotation = next(item for item in arguments if item is not type(None))
  if annotation not in (bool, int, str) and not (
    get_origin(annotation) is Literal
    and all(
      type(value) is str and re.fullmatch(r'[^,\s]+', value) for value in get_args(annotation)
    )
  ):
    raise TypeError(f'unsupported recipe field type {annotation}')
  return annotation, nullable


def validate_field(name: str, value: Any, annotation: Any) -> None:
  annotation, nullable = field_type(annotation)
  if value is None and nullable:
    return
  if get_origin(annotation) is Literal:
    if type(value) is not str or value not in get_args(annotation):
      raise ValueError(f'invalid {name}: must be one of {get_args(annotation)}; got {value!r}')
  elif type(value) is not annotation:
    expected = 'boolean' if annotation is bool else annotation.__name__
    raise TypeError(f'{name} must be {expected}; got {value!r}')
  if isinstance(value, str) and re.fullmatch(r'[^,\s]+', value) is None:
    raise ValueError(f'invalid {name} {value!r}: recipe text cannot write it')
