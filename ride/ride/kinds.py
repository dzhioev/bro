"""broker kinds contributed by installed distributions.

The `bro.broker_kinds` entry-point group is how an installation adds a
capability to every managed session's root broker: each entry names the kind
and targets a factory `(context: KindContext) -> RequestHandler` (the context
type and what it carries: `bro/kinds.py`). The contract uses core types only,
so a contributing distribution needs no ride import; the handler owns its own
per-peer authorization. `run_root_via_broker` registers the loaded kinds
beside the built-ins, where the dispatcher's one-handler-per-kind rule turns
any name collision into a launch failure.
"""

import importlib.metadata
from collections.abc import Callable
from typing import cast

from bro.broker.dispatcher import RequestHandler
from bro.kinds import KindContext

KIND_GROUP = 'bro.broker_kinds'

KindFactory = Callable[[KindContext], RequestHandler]


def _kind_entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
  return tuple(importlib.metadata.entry_points(group=KIND_GROUP))


def extension_kinds(context: KindContext) -> dict[str, RequestHandler]:
  """the contributed kinds for a session under `context`, keyed by kind name
  in registration order (sorted, so it is deterministic across
  installations)."""
  kinds: dict[str, RequestHandler] = {}
  for entry_point in sorted(_kind_entry_points(), key=lambda entry: entry.name):
    if entry_point.name in kinds:
      raise ValueError(f'duplicate broker kind {entry_point.name!r}')
    factory = entry_point.load()
    if not callable(factory):
      raise TypeError(f'broker kind entry point {entry_point.name!r} must load a callable')
    kinds[entry_point.name] = cast(KindFactory, factory)(context)
  return kinds
