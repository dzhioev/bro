from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Self

# the neutral reasoning-effort vocabulary `LLMSpec.with_effort` accepts; each
# provider maps these levels onto its own scale.
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')

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


@dataclass(frozen=True)
class LLMSpec(ABC):
  """recipe for an LLM: model + provider-specific knobs.

  subclasses live alongside their provider (e.g. `bro.llm.llms.openai.LLMSpec`)
  and carry the typed knobs it accepts. each subclass validates its own field
  combinations in `__post_init__` and provides a round-trip `dump` / `from_dict`
  pair keyed by `TYPE` so a stored spec can be reconstructed.

  A recipe is not by itself something the framework can run: a provider whose
  harness drives its own loop (`bro.llm.llms.claude_code`) is a recipe and
  nothing more, while one accepted by the bro-native engine subclasses
  `NativeLLMSpec`. The engine maps that recipe to its client.

  Frozen so a class-level `llm_spec = SomeSpec(...)` default can be shared
  across instances safely — `.fast()` and friends return a new spec via
  `dataclasses.replace` rather than mutating in place.
  """

  # short stable identifier used as the discriminator in `dump` / `from_dict`.
  TYPE: ClassVar[str]

  model: str

  def fast(self) -> Self:
    """return a copy of self with the provider's 'fast' knob set.

    raises NotImplementedError when the provider has no fast-mode equivalent —
    callers should treat that as 'this LLM type does not support fast mode'.
    """
    raise NotImplementedError(f'{self.TYPE} does not support fast mode')

  def with_effort(self, effort: str) -> Self:
    """return a copy of self with the provider's reasoning-effort knob set to the
    given neutral level (`EFFORT_LEVELS`); each provider maps the neutral
    vocabulary onto its own scale.

    raises NotImplementedError when the provider has no effort equivalent, and
    ValueError on a level outside the neutral vocabulary.
    """
    raise NotImplementedError(f'{self.TYPE} does not support an effort override')

  def needed_secrets(self) -> tuple[str, ...]:
    """credentials this spec's provider resolves through the store (e.g. openai
    → `openai`). folded into a bro's hydration set on surfaces that run the bro as
    an LLM process (`bro run` / `bro chat`). default empty for providers with no key."""
    return ()

  @abstractmethod
  def dump(self) -> dict:
    """serialize to a dict including `type` so `LLMSpec.from_dict` can round-trip."""
    ...

  @classmethod
  def from_dict(cls, data: dict) -> 'LLMSpec':
    """reconstruct via the discriminator. dispatches across LLMSpec subclasses."""
    _ensure_providers_loaded()
    type_name = data['type']
    for subclass in _walk_subclasses(LLMSpec):
      if getattr(subclass, 'TYPE', None) == type_name:
        return subclass._from_dict_impl(data)
    raise ValueError(f'unknown LLMSpec type: {type_name!r}')

  @classmethod
  @abstractmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    """build an instance from a dict produced by `dump`. called via `LLMSpec.from_dict`."""
    ...


@dataclass(frozen=True)
class NativeLLMSpec(LLMSpec, ABC):
  """an `LLMSpec` the bro-native engine can run."""


def _ensure_providers_loaded() -> None:
  # `LLMSpec.from_dict` dispatches across `LLMSpec.__subclasses__()`, which only
  # sees classes Python has already imported, so deserialisation has to pull
  # every provider in itself.
  from bro.llm import providers

  providers.load_all()


def _walk_subclasses(cls: type) -> list[type]:
  seen: set[type] = set()
  result: list[type] = []
  stack = [cls]
  while len(stack) > 0:
    c = stack.pop()
    for s in c.__subclasses__():
      if s not in seen:
        seen.add(s)
        result.append(s)
        stack.append(s)
  return result
