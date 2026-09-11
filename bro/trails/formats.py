"""Trail schema format validation and in-memory upgrades."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bro.trails import model


class UnsupportedTrailFormat(ValueError):
  """A stored trail names a schema newer than this reader understands."""


@dataclass(frozen=True)
class FormatUpgrade:
  """The header and row transforms from one format to the next."""

  header: Callable[[dict[str, Any]], dict[str, Any]]
  row: Callable[[dict[str, Any]], dict[str, Any]]


# Key N transforms format N into N + 1.
UPGRADES: dict[int, FormatUpgrade] = {}


def stored_format(record: dict[str, Any], *, description: str) -> int:
  value = record.get('format', model.INITIAL_TRAIL_FORMAT)
  if not isinstance(value, int) or isinstance(value, bool) or value < 1:
    raise ValueError(f'{description} has invalid trail format {value!r}')
  return value


def upgrade_header(header: dict[str, Any]) -> dict[str, Any]:
  return _upgrade(header, description='trail header', transform=lambda step: step.header)


def upgrade_row(row: dict[str, Any]) -> dict[str, Any]:
  trail_id = row.get('trail_id')
  step_id = row.get('step_id')
  description = f'trail row {trail_id}/{step_id}'
  upgraded = _upgrade(
    row,
    description=description,
    transform=lambda step: step.row,
  )
  missing = object()
  if upgraded.get('body', missing) != row.get('body', missing):
    raise ValueError(f'{description} format upgrade changed its immutable body')
  return upgraded


def _upgrade(
  record: dict[str, Any],
  *,
  description: str,
  transform: Callable[[FormatUpgrade], Callable[[dict[str, Any]], dict[str, Any]]],
) -> dict[str, Any]:
  current = stored_format(record, description=description)
  target = model.TRAIL_FORMAT
  if current > target:
    raise UnsupportedTrailFormat(
      f'{description} uses trail format {current}; this reader supports through format {target}'
    )
  upgraded = dict(record)
  while current < target:
    try:
      step = UPGRADES[current]
    except KeyError as exception:
      raise RuntimeError(
        f'missing trail format upgrade from {current} to {current + 1}'
      ) from exception
    upgraded = transform(step)(dict(upgraded))
    if not isinstance(upgraded, dict):
      raise TypeError(f'trail format upgrade from {current} returned a non-object')
    current += 1
    upgraded['format'] = current
  upgraded['format'] = target
  return upgraded
