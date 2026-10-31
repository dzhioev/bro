from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import ClassVar, Self

from bro.llm.records import Stop

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
class LLMSpec(ABC):
  """recipe for an LLM: model + provider-specific knobs.

  Subclasses carry typed provider knobs and validate their field combinations.
  The recipe names the wire a compatible harness can drive.

  Frozen so a class-level `llm_spec = SomeSpec(...)` default can be shared
  across instances safely — `.fast()` and friends return a new spec via
  `dataclasses.replace` rather than mutating in place.
  """

  # short stable identifier used as the discriminator in `dump` / `from_dict`.
  TYPE: ClassVar[str]
  MODEL_INPUTS: ClassVar[Mapping[str, frozenset[str]]]
  WIRE: ClassVar[str]
  ENDPOINT: ClassVar[str | None] = None
  selection_fields: ClassVar[Mapping[str, str]] = MappingProxyType({'model': 'model'})
  deferred_fields: ClassVar[frozenset[str]] = frozenset()
  effort_values: ClassVar[frozenset[str]] = frozenset()

  passthrough: bool = field(default=False, kw_only=True)

  def admitted(self) -> bool:
    model = self.selection()['model']
    return self.passthrough or model is None or model in self.MODEL_INPUTS

  def request_options(self) -> dict:
    return {}

  def is_transient(self, error: Exception) -> bool:
    return False

  def accepts(self, mime_type: str) -> bool:
    model = self.selection()['model']
    return self.passthrough or (
      model is not None and model in self.MODEL_INPUTS and mime_type in self.MODEL_INPUTS[model]
    )

  def selection(self) -> dict[str, str | None]:
    return {name: getattr(self, field) for name, field in self.selection_fields.items()}

  def with_selection(self, values: Mapping[str, str | None]) -> Self:
    if values.keys() != self.selection_fields.keys():
      raise ValueError(f'{self.TYPE} selection fields must be {sorted(self.selection_fields)}')
    for name, value in values.items():
      if value is None and name not in self.deferred_fields:
        raise ValueError(f'{self.TYPE} cannot defer {name}')
      if value is not None and type(value) is not str:
        raise ValueError(f'{self.TYPE} {name} must be a string or a deferred value')
    return replace(self, **{self.selection_fields[name]: value for name, value in values.items()})

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
    """Credentials this provider needs on surfaces running it as an LLM process."""
    return ()

  def dump(self) -> dict:
    """serialize to a dict including `type` so `LLMSpec.from_dict` can round-trip."""
    data = self._dump_impl()
    if self.passthrough:
      data['passthrough'] = True
    return data

  @abstractmethod
  def _dump_impl(self) -> dict: ...

  @classmethod
  def from_dict(cls, data: dict) -> 'LLMSpec':
    """Reconstruct through only the declaration named by the stored type."""
    from bro.llm import providers

    spec = providers.declaration(data['type']).LLMSpec._from_dict_impl(data)
    if 'passthrough' in data:
      if type(data['passthrough']) is not bool:
        raise TypeError('passthrough must be a boolean')
      spec = replace(spec, passthrough=data['passthrough'])
    return spec

  @classmethod
  @abstractmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    """build an instance from a dict produced by `dump`. called via `LLMSpec.from_dict`."""
    ...
