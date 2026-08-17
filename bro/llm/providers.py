"""the LLM provider roster: which providers exist, what a launch calls them, and
how a `provider:model:effort+fast` selection resolves to an `LLMSpec`.

A provider name is the whole vocabulary for one provider — the `--provider`
value, its spec's `TYPE` discriminator, and (where it reads one) its credential
kind — so a launch, a stored spec, and a hydrated scope all spell it the same.

Adding a provider is a module under `llms/` exporting `LLMSpec`,
`DEFAULT_MODEL`, and a `MODELS` short-name table, plus its row in
`_PROVIDER_MODULES`.
"""

import dataclasses
import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Optional

from bro.base import log
from bro.llm.llm import EFFORT_LEVELS, LLMSpec

_PROVIDER_MODULES: dict[str, str] = {
  'openai': 'bro.llm.llms.openai',
  'claude-code': 'bro.llm.llms.claude_code',
  'echo': 'bro.llm.llms.echo',
}

FAST_SUFFIX = '+fast'
_SEPARATOR = ':'
_SLOTS = 3


class LLMSelectionError(ValueError):
  """a launch named an LLM that cannot be resolved: an unknown provider, model,
  or preset, or a malformed `--llm` value. The message is operator-facing."""


def known_names() -> tuple[str, ...]:
  return tuple(_PROVIDER_MODULES)


def _provider_module(provider: str) -> ModuleType:
  path = _PROVIDER_MODULES.get(provider)
  if path is None:
    raise LLMSelectionError(
      f'unknown provider {provider!r}; known providers: {", ".join(known_names())}'
    )
  return importlib.import_module(path)


def load_all() -> None:
  """import every provider module, for callers that need the classes themselves
  rather than a named one (`LLMSpec.from_dict`'s subclass dispatch)."""
  for provider in known_names():
    _provider_module(provider)


def models(provider: str) -> dict[str, str]:
  """the provider's short model names, each mapped to the full id it stands for."""
  return _provider_module(provider).MODELS


def default_spec(provider: str) -> LLMSpec:
  """the provider's own default recipe — its default model and no knobs."""
  return _provider_module(provider).LLMSpec()


def resolve_model(provider: str, model: str) -> str:
  """the full model id `model` names within `provider`.

  The short-name table is a convenience, not a whitelist: an unlisted name is
  the id itself, so a model the table has not learned still launches.
  """
  return models(provider).get(model, model)


def provider_of_model(model: str) -> str:
  """the provider whose roster carries `model`, by short name or full id.

  Raises when no provider claims it or more than one does — either way the
  launch has to name the provider itself.
  """
  matches = [
    provider
    for provider in known_names()
    if model in models(provider) or model in models(provider).values()
  ]
  if len(matches) == 1:
    return matches[0]
  if len(matches) == 0:
    raise LLMSelectionError(
      f'unknown model {model!r}; name its provider, or use one of: {", ".join(_all_short_names())}'
    )
  raise LLMSelectionError(
    f'model {model!r} is served by more than one provider ({", ".join(matches)}); name one'
  )


def _all_short_names() -> list[str]:
  return sorted(name for provider in known_names() for name in models(provider))


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
    if self.provider is not None and self.provider not in _PROVIDER_MODULES:
      raise LLMSelectionError(
        f'unknown provider {self.provider!r}; known providers: {", ".join(known_names())}'
      )
    if self.effort is not None and self.effort not in EFFORT_LEVELS:
      raise LLMSelectionError(
        f'unknown effort level {self.effort!r}; expected one of {", ".join(EFFORT_LEVELS)}'
      )
    if self.provider is None and self.model is not None:
      # with no provider named, the model is the only thing that can name one —
      # so it has to be a name some roster actually carries
      provider_of_model(self.model)

  def is_empty(self) -> bool:
    return self == LLMSelection()

  def provider_name(self) -> Optional[str]:
    """the provider this selection runs, named or inferred from the model —
    None when it names neither, leaving the standing recipe's provider."""
    if self.provider is not None:
      return self.provider
    if self.model is not None:
      return provider_of_model(self.model)
    return None

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


def resolve(base: LLMSpec, selection: LLMSelection) -> LLMSpec:
  """the recipe a launch runs: `selection` applied over the standing `base`.

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
    provider = provider_of_model(selection.model)
    model = resolve_model(provider, selection.model)
    spec = (
      dataclasses.replace(base, model=model)
      if type(base) is type(default_spec(provider))
      else dataclasses.replace(default_spec(provider), model=model)
    )
  if selection.fast:
    try:
      spec = spec.fast()
    except NotImplementedError:
      log.verbose('%s has no fast mode; running with the plain recipe', spec.TYPE)
  if selection.effort is not None:
    spec = spec.with_effort(selection.effort)
  return spec
