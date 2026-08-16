#!/usr/bin/env python

import asyncio
import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Optional, Self

import bro.base.args as base_args
from bro.llm.mcp import MCPServer, ToolRegistry
from bro.llm.observer import NullObserver, Observer
from bro.llm.tracker import NullTracker, Tracker


class LLM(ABC):
  def __init__(
    self,
    mcp_servers: Optional[list[MCPServer]] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ):
    self.tools = ToolRegistry(mcp_servers if mcp_servers is not None else [])
    self.observer: Observer = observer if observer is not None else NullObserver()
    # sibling of observer — records the run for offline analysis instead of
    # rendering it to stderr. swapped in via LLMSpec.create_llm by BaseBro so
    # the bro and the LLM share one Tracker per trail.
    self.tracker: Tracker = tracker if tracker is not None else NullTracker()
    # the surface identity (e.g. the bro name) stamped on published usage
    # snapshots; None disables publishing to the env-pointed usage file.
    self.agent = agent

  @abstractmethod
  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str: ...

  def cumulative_usage(self) -> Optional[dict[str, dict[str, int]]]:
    """per-model counts in the four billed token classes (`bro.llm.usage.CLASSES`),
    summed over this instance's lifetime; None when the provider doesn't
    track usage."""
    return None


# the neutral reasoning-effort vocabulary `LLMSpec.with_effort` accepts; each
# provider maps these levels onto its own scale.
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')


@dataclass(frozen=True)
class LLMSpec(ABC):
  """recipe for an LLM: model + provider-specific knobs.

  subclasses live alongside their provider (e.g. `bro.llm.llms.openai.LLMSpec`)
  and carry the typed knobs it accepts. each subclass validates its own field
  combinations in `__post_init__` and provides a round-trip `dump` / `from_dict`
  pair keyed by `TYPE` so a stored spec can be reconstructed.

  A recipe is not by itself something the framework can run: a provider whose
  harness drives its own loop (`bro.llm.llms.claude_code`) is a recipe and
  nothing more, while one the bro-native loop drives subclasses `NativeLLMSpec`
  and builds the client.

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
  """an `LLMSpec` the bro-native loop can run: `create_llm` builds the in-process
  client a turn is sent through."""

  @abstractmethod
  def create_llm(
    self,
    mcp_servers: Optional[list[MCPServer]] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ) -> LLM: ...


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


async def llm_main(request: str, provider: str, model: Optional[str], attachments: list[str]):
  from bro.llm import providers as llm_providers

  spec = llm_providers.default_spec(provider)
  if not isinstance(spec, NativeLLMSpec):
    raise ValueError(f'provider {provider!r} builds no in-process client')
  if model is not None:
    spec = dataclasses.replace(spec, model=llm_providers.resolve_model(provider, model))
  instance = spec.create_llm()
  content: list[dict] = [{'type': 'text', 'text': request}]
  for path in attachments:
    content.append({'type': 'image_url', 'image_url': {'url': path}})
  messages: list[dict] = [{'role': 'user', 'content': content}]
  print(f'> {request}')
  print()
  response = await instance.send(messages)
  print(f'< {response}')


def main(argv: list[str]) -> Optional[int]:
  from bro.llm import providers as llm_providers

  parser = base_args.Parser(description='chat with LLM')
  parser.add_argument('--attach', '-a', dest='attachments', nargs='*', default=[])
  parser.add_argument(
    '--provider', '-p', choices=llm_providers.known_names(), default='echo', help='LLM provider'
  )
  parser.add_argument(
    '--model', '-m', default=None, help='model short name or id within --provider'
  )
  parser.add_argument('request')
  return asyncio.run(llm_main(**parser.parse(argv)))
