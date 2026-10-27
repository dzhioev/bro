"""the LLM provider roster: which providers exist, what a launch calls them, and
how a `provider:model:effort+fast` selection resolves to an `LLMSpec`.

A provider name is the whole vocabulary for one provider — the `--provider`
value, its spec's `TYPE` discriminator, and (where it reads one) its credential
kind — so a launch, a stored spec, and a hydrated scope all spell it the same.

Provider declarations are discovered through installation metadata and loaded only
when selected or searched for their advertised capabilities.
"""

import dataclasses
import functools
import importlib.metadata
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import ModuleType
from typing import Any, Optional

from bro.base import log
from bro.llm.llm import EFFORT_LEVELS, FailureSignature, LLMSpec

ENTRY_POINT_GROUP = 'bro.llm.providers'

FAST_SUFFIX = '+fast'
_SEPARATOR = ':'
_SLOTS = 3


class LLMSelectionError(ValueError):
  """a launch named an LLM that cannot be resolved: an unknown provider, model,
  or preset, or a malformed `--llm` value. The message is operator-facing."""


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
    raise LLMSelectionError(
      f'unknown provider {provider!r}; installed providers: {", ".join(entries) or "(none)"}; '
      'install the plug-in that provides it'
    )
  try:
    module = entries[provider].load()
    if not isinstance(module, ModuleType):
      raise TypeError('entry must load a declaration module')
    if not issubclass(module.LLMSpec, LLMSpec) or module.LLMSpec.TYPE != provider:
      raise ValueError('declaration LLMSpec.TYPE must equal the entry name')
    if not isinstance(module.DEFAULT_MODEL, str) or not isinstance(module.LLMSpec.WIRE, str):
      raise TypeError('declaration must name a default model and a wire')
    if not isinstance(module.MODELS, Mapping) or any(
      not isinstance(name, str) or not isinstance(model, str)
      for name, model in module.MODELS.items()
    ):
      raise TypeError('declaration MODELS must map short names to model ids')
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
    raise LLMSelectionError(f'provider {provider!r} is unavailable: {error}') from error
  return module


def declarations(among: Collection[str] | None = None) -> Iterator[tuple[str, ModuleType]]:
  for name in known_names():
    if among is not None and name not in among:
      continue
    try:
      module = declaration(name)
    except LLMSelectionError as error:
      log.warning('%s', error)
      continue
    yield name, module


def available_specs() -> Iterator[tuple[str, LLMSpec]]:
  for name, module in declarations():
    try:
      spec = module.LLMSpec()
    except Exception as error:
      log.warning('provider %r is unavailable: %s', name, error)
      continue
    yield name, spec


def models(provider: str) -> dict[str, str]:
  """the provider's short model names, each mapped to the full id it stands for."""
  return declaration(provider).MODELS


def default_spec(provider: str) -> LLMSpec:
  """the provider's own default recipe — its default model and no knobs."""
  module = declaration(provider)
  try:
    return module.LLMSpec()
  except Exception as error:
    raise LLMSelectionError(f'provider {provider!r} is unavailable: {error}') from error


def effort_values() -> frozenset[str]:
  return frozenset(value for _, spec in available_specs() for value in spec.effort_values)


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


def resolve_model(provider: str, model: str) -> str:
  """the full model id `model` names within `provider`.

  The short-name table is a convenience, not a whitelist: an unlisted name is
  the id itself, so a model the table has not learned still launches — once it
  has the shape of one, where the provider declares that shape (`MODEL_ID`).
  """
  table = models(provider)
  resolved = table.get(model, model)
  shape = getattr(declaration(provider), 'MODEL_ID', None)
  if shape is not None and shape.fullmatch(resolved) is None:
    raise LLMSelectionError(
      f'{model!r} names no {provider} model id; name a full id or one of: {", ".join(table)}'
    )
  return resolved


def carriers(model: str, among: Collection[str]) -> tuple[str, ...]:
  """the providers in `among` whose rosters carry `model`, by short name or full id."""
  return tuple(
    provider
    for provider, module in declarations(among)
    if model in module.MODELS or model in module.MODELS.values()
  )


def provider_of_model(model: str, among: Collection[str]) -> str:
  """the one provider in `among` whose roster carries `model`.

  Raises when none of them claims it or more than one does — either way the
  launch has to name the provider itself.
  """
  matches = carriers(model, among)
  if len(matches) == 1:
    return matches[0]
  if len(matches) == 0:
    raise LLMSelectionError(
      f'unknown model {model!r}; name its provider, or use one of: {", ".join(_short_names(among))}'
    )
  raise LLMSelectionError(
    f'model {model!r} is served by more than one provider ({", ".join(matches)}); name one'
  )


def _short_names(among: Collection[str]) -> list[str]:
  return sorted(name for _, module in declarations(among) for name in module.MODELS)


@dataclass(frozen=True)
class LLMSelection:
  """a launch's LLM choice as its flags spell it.

  Every field is optional, and an unset one leaves the standing recipe's own
  value — the bro's `llm_spec` on a native launcher, the surface's default on a
  claude session.
  """

  provider: Optional[str] = None
  model: Optional[str] = None
  effort: Optional[str] = None
  fast: bool = False

  def __post_init__(self):
    if self.provider is not None and self.provider not in known_names():
      raise LLMSelectionError(
        f'unknown provider {self.provider!r}; installed providers: {", ".join(known_names())}; '
        'install the plug-in that provides it'
      )
    if self.effort is not None and self.effort not in EFFORT_LEVELS:
      raise LLMSelectionError(
        f'unknown effort level {self.effort!r}; expected one of {", ".join(EFFORT_LEVELS)}'
      )
    if self.provider is None and self.model is not None and not carriers(self.model, known_names()):
      # with no provider named, the model is the only thing that can name one —
      # so it has to be a name some roster actually carries
      raise LLMSelectionError(
        f'unknown model {self.model!r}; name its provider, or use one of: '
        f'{", ".join(_short_names(known_names()))}'
      )

  def is_empty(self) -> bool:
    return self == LLMSelection()

  def providers(self, among: Collection[str]) -> tuple[str, ...]:
    """the providers in `among` this selection can run on: the one it names, or
    those whose rosters carry its model — all of `among` when it names neither,
    leaving the standing recipe's provider."""
    if self.provider is not None:
      return (self.provider,) if self.provider in among else ()
    if self.model is not None:
      return carriers(self.model, among)
    return tuple(among)

  def over(self, base: 'LLMSelection') -> 'LLMSelection':
    """this selection with `base` filling what it leaves unnamed — a launch's
    flags over a configured default.

    Provider and model are one slot: a selection naming either keeps its own
    pair, since a model belongs to its provider and a provider named alone
    selects its default model. Effort fills on its own, and fast holds where
    either names it.
    """
    names_recipe = self.provider is not None or self.model is not None
    return LLMSelection(
      provider=self.provider if names_recipe else base.provider,
      model=self.model if names_recipe else base.model,
      effort=self.effort if self.effort is not None else base.effort,
      fast=self.fast or base.fast,
    )

  def format(self) -> str:
    """this selection as a canonical `--llm` value, which `parse` reads back.

    What a launch surface forwards inward, so the inner run resolves nothing —
    no preset table, no guessing, the same recipe whichever side reads it.
    """
    slots = [self.provider or '', self.model or '', self.effort or '']
    while len(slots) > 0 and slots[-1] == '':
      slots.pop()
    return _SEPARATOR.join(slots) + (FAST_SUFFIX if self.fast else '')


def parse(value: str) -> LLMSelection:
  """read a `--llm` value: `provider:model:effort`, any slot left empty, with an
  optional `+fast` suffix (`:fable5`, `::high`, `openai:sol:max+fast`)."""
  body = value
  fast = body.endswith(FAST_SUFFIX)
  if fast:
    body = body[: -len(FAST_SUFFIX)]
  if '+' in body:
    raise LLMSelectionError(
      f'malformed --llm value {value!r}: the only supported suffix is {FAST_SUFFIX!r}'
    )
  slots = body.split(_SEPARATOR)
  if len(slots) > _SLOTS:
    raise LLMSelectionError(
      f'malformed --llm value {value!r}: expected at most {_SLOTS} '
      f'{_SEPARATOR!r}-separated fields (provider, model, effort)'
    )
  slots += [''] * (_SLOTS - len(slots))
  provider, model, effort = (slot.strip() or None for slot in slots)
  selection = LLMSelection(provider=provider, model=model, effort=effort, fast=fast)
  if selection.is_empty():
    raise LLMSelectionError(f'--llm value {value!r} names nothing')
  return selection


def resolve(base: LLMSpec | None, selection: LLMSelection, among: Collection[str]) -> LLMSpec:
  """the recipe a launch runs: `selection` applied over the standing `base`,
  with a model named alone resolved among the providers in `among`.

  `provider` names a whole recipe — that provider's default model and no knobs —
  while `model` alone names only the model, keeping `base`'s other knobs when it
  belongs to `base`'s own provider. `effort` and `fast` then apply on top, so
  they adjust whichever recipe the first two settled on.

  The two knobs differ on a provider that lacks them: fast is a portable best-effort
  service-tier preference and falls back to the plain recipe, while an effort override
  requests an exact reasoning control and raises when the provider has none.
  """
  spec = base
  if selection.provider is not None:
    spec = default_spec(selection.provider)
    if selection.model is not None:
      spec = dataclasses.replace(spec, model=resolve_model(selection.provider, selection.model))
  elif selection.model is not None:
    provider = provider_of_model(selection.model, among)
    model = resolve_model(provider, selection.model)
    spec = (
      dataclasses.replace(base, model=model)
      if base is not None and type(base) is type(default_spec(provider))
      else dataclasses.replace(default_spec(provider), model=model)
    )
  if spec is None:
    raise LLMSelectionError('no LLM recipe: pass --llm provider:model')
  if selection.fast:
    try:
      spec = spec.fast()
    except NotImplementedError:
      log.verbose('%s has no fast mode; running with the plain recipe', spec.TYPE)
  if selection.effort is not None:
    spec = spec.with_effort(selection.effort)
  return spec
