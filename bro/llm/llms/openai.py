# Every bro module constructs an `LLMSpec` at class-definition time, so this
# declaration module must not import the provider SDK or native client.

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import ClassVar, Literal, Optional, Self, cast, get_args

import bro.llm.llm as llm_llm

ServiceTier = Literal['auto', 'default', 'flex', 'priority']
_VALID_SERVICE_TIERS: frozenset[str] = frozenset(get_args(ServiceTier))
# local mirror of the Literal inside openai's `ReasoningEffort` (which the SDK
# wraps in Optional); spelled out so spec validation needs no openai import. a
# sync test asserts the values against the SDK's type.
ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
_VALID_REASONING_EFFORTS: frozenset[str] = frozenset(get_args(ReasoningEffort))

DEFAULT_MODEL = 'gpt-5.6-terra'

# `--model` short names for this provider's models.
MODELS: dict[str, str] = {
  'terra': 'gpt-5.6-terra',
  'sol': 'gpt-5.6-sol',
}

# how this provider's client failures read in a run's error output
FAILURE_SIGNATURES: tuple[llm_llm.FailureSignature, ...] = (
  llm_llm.FailureSignature(r'openai\.RateLimitError', 'rate-limit'),
  llm_llm.FailureSignature(r'openai\.InternalServerError', 'server-error'),
  llm_llm.FailureSignature(r'openai\.(APIConnectionError|APITimeoutError)', 'network'),
  llm_llm.FailureSignature(
    r'openai\.(AuthenticationError|PermissionDeniedError)', 'authentication'
  ),
  llm_llm.FailureSignature(r'openai\.NotFoundError', 'model-not-found'),
  llm_llm.FailureSignature(r'insufficient_quota', 'usage-limit'),
  llm_llm.FailureSignature(r'openai\.APIStatusError', 'unknown-api'),
)

# neutral effort level (`LLMSpec.with_effort`) → Responses API reasoning_effort.
_EFFORT_TO_REASONING_EFFORT: dict[str, ReasoningEffort] = {
  'low': 'low',
  'medium': 'medium',
  'high': 'high',
  'xhigh': 'xhigh',
  'max': 'max',
}


@dataclass(frozen=True)
class LLMSpec(llm_llm.NativeLLMSpec):
  """spec for the OpenAI Responses API.

  service_tier='priority' is the analog of Claude Code's /fast — same model
  and quality, higher per-token price, faster and more consistent generation.
  Toggle it through `.fast()` rather than constructing a new spec by hand.

  compact_threshold (opt-in) bounds context growth in long runs: the native
  client passes it as server-side context-management policy. None (the default)
  leaves growth unbounded. GPT-5-family models take at most 272k input tokens (400k window
  minus the 128k output reservation), so a value like 200_000 leaves tool-loop
  turns room to grow between the threshold crossing and the compaction pass.
  Size it far above per-turn growth: with the threshold near the working
  context size the server recompacts repeatedly within one response (observed
  live: 10 passes per call, ~5x billed input, minutes of latency).
  """

  TYPE: ClassVar[str] = 'openai'

  model: str = DEFAULT_MODEL
  reasoning_effort: Optional[ReasoningEffort] = None
  service_tier: Optional[ServiceTier] = None
  compact_threshold: Optional[int] = None

  def __post_init__(self):
    if self.service_tier is not None and self.service_tier not in _VALID_SERVICE_TIERS:
      raise ValueError(
        f'invalid service_tier {self.service_tier!r}; expected one of '
        f'{sorted(_VALID_SERVICE_TIERS)} or None'
      )
    if self.reasoning_effort is not None and self.reasoning_effort not in _VALID_REASONING_EFFORTS:
      raise ValueError(
        f'invalid reasoning_effort {self.reasoning_effort!r}; expected one of '
        f'{sorted(_VALID_REASONING_EFFORTS)} or None'
      )
    if self.compact_threshold is not None and self.compact_threshold <= 0:
      raise ValueError(
        f'invalid compact_threshold {self.compact_threshold!r}; expected a positive int or None'
      )

  def fast(self) -> Self:
    return dataclasses.replace(self, service_tier='priority')

  def with_effort(self, effort: str) -> Self:
    reasoning_effort = _EFFORT_TO_REASONING_EFFORT.get(effort)
    if reasoning_effort is None:
      raise ValueError(
        f'unknown effort level {effort!r}; expected one of {list(_EFFORT_TO_REASONING_EFFORT)}'
      )
    return dataclasses.replace(self, reasoning_effort=reasoning_effort)

  def needed_secrets(self) -> tuple[str, ...]:
    return ('openai',)

  def dump(self) -> dict:
    return {
      'type': self.TYPE,
      'model': self.model,
      'reasoning_effort': self.reasoning_effort,
      'service_tier': self.service_tier,
      'compact_threshold': self.compact_threshold,
    }

  @classmethod
  def _from_dict_impl(cls, data: dict) -> LLMSpec:
    # __post_init__ revalidates these against the Literal types; the cast keeps
    # the static checker happy on the JSON-derived path where pyright sees
    # `str | None`.
    return cls(
      model=data['model'],
      reasoning_effort=cast(Optional[ReasoningEffort], data.get('reasoning_effort')),
      service_tier=cast(Optional[ServiceTier], data.get('service_tier')),
      compact_threshold=data.get('compact_threshold'),
    )
