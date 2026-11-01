"""Authority records for launch-boundary tests."""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from bro.authority.authority import Authority
from bro.authority.rules import Mission
from ride.authority_launch import request
from ride.authority_reading import Configuration, Installation, Reading
from ride.bro_worker import BroType


def reading(*, declarations=None, config=None) -> Reading:
  from bro.openai.declaration import Recipe

  installed = Installation.take()
  installed = replace(
    installed,
    harnesses={
      name: replace(
        driver,
        recipes={
          target: Recipe(reasoning_effort='medium') if recipe is None else recipe
          for target, recipe in driver.recipes.items()
        },
      )
      for name, driver in installed.harnesses.items()
    },
  )
  if declarations is not None:
    installed = replace(installed, declarations={**installed.declarations, **declarations})
  return Reading(
    Configuration.parse({'harness': 'claude', 'summon-harness': 'bro', **(config or {})}),
    {},
    installed,
  )


def mission(
  *,
  grant: Iterable[str] = (),
  revoke: Iterable[str] = (),
  limit: Iterable[str] = (),
  target: str = 'bro',
  source: Reading | None = None,
  values: Mapping[str, Any] | None = None,
  top: bool = False,
) -> Mission:
  source = source or reading()
  requested, _ = request(
    BroType,
    {
      'target': target,
      **(values or {}),
      'grant': list(grant),
      'revoke': list(revoke),
      'limit': list(limit),
    },
    source,
    source.operator(),
    forward=False,
  )
  result = source.rules.start(requested, source.operator(), source)
  return replace(result, authority=Authority.top()) if top else result
