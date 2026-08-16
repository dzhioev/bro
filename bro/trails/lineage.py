"""Shared fork-lineage traversal and the harness resolution verdict."""

import dataclasses
from collections.abc import Callable
from typing import Any, Optional


@dataclasses.dataclass(frozen=True)
class LineageDecision:
  """A harness resolver's verdict for a trail about to be blazed: where it forks
  from, and which ranges of the harness artifact the trail will hold. Each chunk
  is a half-open ``[start, end)`` range; the last one grows as the artifact does.
  ``adopt`` is False when the artifact is not yet in a state worth recording, and
  nothing is created."""

  adopt: bool = True
  forked_from: Optional[dict[str, Any]] = None
  chunks: list[list[int]] = dataclasses.field(default_factory=lambda: [[0, 0]])
  reason: Optional[str] = None


def walk_chain[TrailValue, BoundValue](
  trail: TrailValue,
  *,
  identity: Callable[[TrailValue], str],
  parent: Callable[[TrailValue], Optional[tuple[str, BoundValue]]],
  fetch_parent: Callable[[str], TrailValue],
) -> list[tuple[TrailValue, Optional[BoundValue]]]:
  """Return a fork chain root-first with each ancestor's inclusive child bound."""
  segments: list[tuple[TrailValue, Optional[BoundValue]]] = [(trail, None)]
  trail_id = identity(trail)
  seen = {trail_id}
  current = trail
  while True:
    parent_pointer = parent(current)
    if parent_pointer is None:
      break
    parent_id, bound = parent_pointer
    if parent_id in seen:
      raise ValueError(f'fork chain of trail {trail_id} cycles through {parent_id}')
    seen.add(parent_id)
    current = fetch_parent(parent_id)
    fetched_id = identity(current)
    if fetched_id != parent_id:
      raise ValueError(f'lineage lookup for {parent_id!r} returned trail {fetched_id!r}')
    segments.append((current, bound))
  segments.reverse()
  return segments


def walk_header_chain(
  trail: dict, fetch_parent: Callable[[str], dict]
) -> list[tuple[dict, Optional[dict]]]:
  """Walk wire-format trail headers through their ``forked_from`` pointers."""

  def parent(header: dict) -> Optional[tuple[str, dict]]:
    pointer = header.get('forked_from')
    if pointer is None:
      return None
    step_id = pointer.get('step_id') if isinstance(pointer, dict) else None
    if (
      not isinstance(pointer, dict)
      or not isinstance(pointer.get('trail_id'), str)
      or not isinstance(step_id, int)
      or isinstance(step_id, bool)
    ):
      raise ValueError(f'trail {header.get("id")!r} has malformed forked_from')
    return pointer['trail_id'], pointer

  return walk_chain(
    trail,
    identity=lambda header: header['id'],
    parent=parent,
    fetch_parent=fetch_parent,
  )
