"""Shared fork-lineage traversal, the header's lineage head, and the harness resolution verdict."""

import dataclasses
from collections.abc import Callable
from typing import Any, NamedTuple, Optional

# how many of a trail's trailing uuid rows its head remembers: a history copy
# drops ephemeral records, so an intact copy can be missing the very last of
# them, and the window is what spans that gap
_TAIL_ROWS = 20

_HEAD_FIELDS = frozenset({'chain_first_uuid', 'tail', 'last_row_digest'})


class TailRow(NamedTuple):
  """One remembered uuid-carrying row."""

  step_id: int
  uuid: str
  payload_sha256: str


@dataclasses.dataclass
class LineageHead:
  """What a trail's header remembers of its rows, so a resume's lineage resolves
  against headers alone: the conversation's first record uuid — set at the root
  and inherited at every fork, since no single trail's rows hold it once a copy
  is skipped — the newest uuid rows, and the digest of the final row, which may
  carry no uuid of its own."""

  chain_first_uuid: Optional[str] = None
  tail: list[TailRow] = dataclasses.field(default_factory=list)
  last_row_digest: Optional[str] = None

  @classmethod
  def stored(cls, native: dict) -> 'LineageHead':
    """The head a trail's ``native`` carries, empty where it carries none."""
    value = native.get('lineage_head')
    if value is None:
      return cls()
    if not isinstance(value, dict) or len(set(value) - _HEAD_FIELDS) > 0:
      raise ValueError(f'malformed lineage head: {value!r}')
    tail = value.get('tail', [])
    if not isinstance(tail, list):
      raise ValueError(f'malformed lineage head tail: {tail!r}')
    return cls(
      chain_first_uuid=_optional_text(value.get('chain_first_uuid'), 'chain_first_uuid'),
      tail=[_tail_row(entry) for entry in tail],
      last_row_digest=_optional_text(value.get('last_row_digest'), 'last_row_digest'),
    )

  def inherited(self) -> 'LineageHead':
    """The head a fork of this trail opens with, and the one a re-fold of its own
    rows starts from."""
    return LineageHead(chain_first_uuid=self.chain_first_uuid)

  def fold(self, *, step_id: int, uuid: Optional[str], payload_sha256: str) -> None:
    """Carry the head past one more row."""
    self.last_row_digest = payload_sha256
    if uuid is None:
      return
    if self.chain_first_uuid is None:
      self.chain_first_uuid = uuid
    self.tail.append(TailRow(step_id, uuid, payload_sha256))
    del self.tail[:-_TAIL_ROWS]

  def fields(self) -> dict[str, Any]:
    """The value a header stores it as."""
    return {
      'chain_first_uuid': self.chain_first_uuid,
      'tail': [list(row) for row in self.tail],
      'last_row_digest': self.last_row_digest,
    }


def _optional_text(value: Any, field: str) -> Optional[str]:
  if value is not None and not isinstance(value, str):
    raise ValueError(f'lineage head {field} must be a string or null')
  return value


def _tail_row(entry: Any) -> TailRow:
  if (
    not isinstance(entry, list)
    or len(entry) != 3
    or not isinstance(entry[0], int)
    or isinstance(entry[0], bool)
    or not all(isinstance(item, str) for item in entry[1:])
  ):
    raise ValueError(f'lineage head tail row must be [step_id, uuid, payload_sha256]: {entry!r}')
  return TailRow(*entry)


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
