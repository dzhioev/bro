"""Claude fork resolution: which recorded trail an adopted transcript continues.

Claude Code writes one jsonl *segment* per session id. An interactive resume
forks a fresh segment whose file re-serializes the conversation history — record
`uuid`s and `timestamp`s preserved, `sessionId` rewritten, ephemeral records
dropped — before the new turns; a headless resume appends to the existing
segment. A trail is the slice of one segment that one recorder lifetime
uploaded, so every resume opens a new trail that either continues a recorded one
or starts a root.

The evidence a recorder ships is the segment name, one `[uuid | null,
payload_sha256]` pair per transcript line, and the names of sibling segment
files sharing its records. A history copy rewrites `sessionId`, so a record uuid
is the only link from a line back to the row that already holds it.

Resolution reads headers only: a candidate is any trail recording the adopted
segment or a sibling, and what it is verified against is the `lineage_head` its
own rows folded (`lineage.py`). Two continuations are verifiable:

- a same-segment resume starts after the recorded extent and forks from the
  prior trail's final row — every remembered uuid must be present in the file and
  hash to the line it is found on, and the final row, placed from the newest of
  them, must hash to `last_row_digest`;
- a new-segment resume forks at the newest remembered uuid the copy still holds
  and keeps only the new segment's own contribution (pre-copy ephemera plus the
  post-copy tail). The copy is located by the conversation's first record, which
  every copy in the chain re-serializes; digests take no part here at all, since
  the rewritten `sessionId` changes every copied line's.

Claude writes a forked segment in stages — head ephemera, then the re-serialized
history, then the new turns — so a copy read mid-write carries no line of its
own yet: the file ends at the anchor, or it reaches no remembered uuid while its
newest record is one the segment family already stores. `/clear` and failed
verification both yield a root rather than an invented edge.
"""

import dataclasses
from typing import Any, Optional, Protocol

from bro.trails.lineage import LineageDecision, LineageHead


class LineageIndex(Protocol):
  """The lookups a store offers a lineage resolver, always in process."""

  def find_segment_trails(self, segments: set[str]) -> list[dict]:
    """The headers of the trails recording any of `segments`."""
    ...

  def holds_record(self, trail_ids: set[str], uuid: str) -> bool:
    """Whether any of the named trails stores the record `uuid`."""
    ...


@dataclasses.dataclass(frozen=True)
class _Evidence:
  segment: str
  related: frozenset[str]
  hashes: list[str]
  uuid_lines: list[tuple[int, str]]

  @property
  def line_count(self) -> int:
    return len(self.hashes)

  def line_by_uuid(self) -> dict[str, int]:
    lines: dict[str, int] = {}
    for line, uuid in self.uuid_lines:
      lines.setdefault(uuid, line)
    return lines


class _NewestRecordProbe:
  """The one uuid query a resolution may need: whether the file's newest record
  is one the segment family already stores, which is what a history copy looks
  like before it reaches its own new turns. Asked at most once, for every
  candidate."""

  def __init__(self, index: LineageIndex, family: set[str], uuid: str) -> None:
    self._index = index
    self._family = family
    self._uuid = uuid
    self._answer: Optional[bool] = None

  def stored_by_family(self) -> bool:
    if self._answer is None:
      self._answer = self._index.holds_record(self._family, self._uuid)
    return self._answer


def _copy_in_progress() -> LineageDecision:
  return LineageDecision(adopt=False, reason='history copy still being written')


def resolve(evidence: dict, index: LineageIndex) -> LineageDecision:
  """Decide the lineage of a trail about to record the transcript `evidence`
  describes."""
  parsed = _parse_evidence(evidence)
  if len(parsed.uuid_lines) == 0:
    return LineageDecision(adopt=False, reason='transcript carries no record yet')
  candidates = _candidates(parsed, index)
  probe = _NewestRecordProbe(
    index, {header['id'] for header in candidates}, parsed.uuid_lines[-1][1]
  )
  for header in candidates:
    head = LineageHead.stored(header['native'])
    if header['native'].get('segment') == parsed.segment:
      decision = _continue_segment(header, head, parsed)
    else:
      decision = _continue_copy(header, head, parsed, probe)
    if decision is not None:
      return decision
  return LineageDecision(reason='no verified parent')


def _parse_evidence(evidence: dict) -> _Evidence:
  segment = evidence.get('segment')
  lines = evidence.get('lines')
  related = evidence.get('related_segments', [])
  if not isinstance(segment, str) or len(segment) == 0:
    raise ValueError('lineage evidence needs a non-empty segment')
  if not isinstance(lines, list):
    raise ValueError('lineage evidence needs a list of lines')
  if not isinstance(related, list) or not all(isinstance(name, str) for name in related):
    raise ValueError('lineage related_segments must be a list of strings')
  hashes: list[str] = []
  uuid_lines: list[tuple[int, str]] = []
  for line, entry in enumerate(lines):
    if (
      not isinstance(entry, list)
      or len(entry) != 2
      or (entry[0] is not None and not isinstance(entry[0], str))
      or not isinstance(entry[1], str)
    ):
      raise ValueError(f'lineage line {line} must be [uuid or null, payload_sha256]')
    uuid, digest = entry
    hashes.append(digest)
    if uuid is not None:
      uuid_lines.append((line, uuid))
  return _Evidence(
    segment=segment, related=frozenset(related), hashes=hashes, uuid_lines=uuid_lines
  )


def _candidates(evidence: _Evidence, index: LineageIndex) -> list[dict]:
  """The trails that could be this transcript's parent, newest first: the ones
  recording its segment, and the ones recording a sibling segment its records
  came from."""
  headers = index.find_segment_trails({evidence.segment, *evidence.related})
  for header in headers:
    if (
      not isinstance(header.get('id'), str)
      or not isinstance(header.get('started_at'), str)
      or not isinstance(header.get('native'), dict)
    ):
      raise ValueError(f'malformed lineage index trail: {header!r}')
  return sorted(headers, key=lambda header: header['started_at'], reverse=True)


def _continue_segment(
  header: dict, head: LineageHead, evidence: _Evidence
) -> Optional[LineageDecision]:
  """Claude appended to the segment this trail recorded: the new trail starts
  past the recorded extent and forks from the prior trail's final row."""
  extent = _extent(header)
  line_by_uuid = evidence.line_by_uuid()
  found = [(row, line_by_uuid[row.uuid]) for row in head.tail if row.uuid in line_by_uuid]
  # a remembered uuid the file no longer carries means it lost recorded lines
  if len(head.tail) == 0 or len(found) != len(head.tail):
    return None
  # a remembered uuid whose line hashes differently is a copy of that row, not it
  if any(evidence.hashes[line] != row.payload_sha256 for row, line in found):
    return None
  anchor, anchor_line = found[-1]
  # rows past the last remembered one carry no uuid and follow it in the file: a
  # trail's stream is split only where it skipped a history copy, which is
  # always before its own first record
  last_line = anchor_line + (extent - 1 - anchor.step_id)
  if last_line >= evidence.line_count or evidence.hashes[last_line] != head.last_row_digest:
    return None
  if last_line + 1 == evidence.line_count:
    return LineageDecision(adopt=False, reason='no line past the recorded extent yet')
  return LineageDecision(
    forked_from={'trail_id': header['id'], 'step_id': extent - 1},
    chunks=[[last_line + 1, last_line + 1]],
  )


def _continue_copy(
  header: dict, head: LineageHead, evidence: _Evidence, probe: _NewestRecordProbe
) -> Optional[LineageDecision]:
  """Claude forked a new segment re-serializing the history: locate the copy by
  the conversation's first record, anchor the fork at the newest remembered row
  the file still holds, and keep only the new segment's own contribution — the
  recorded chain is authoritative for the copied part."""
  line_by_uuid = evidence.line_by_uuid()
  if head.chain_first_uuid is None:
    return None
  copy_start_line = line_by_uuid.get(head.chain_first_uuid)
  if copy_start_line is None:
    return None
  anchor = next((row for row in reversed(head.tail) if row.uuid in line_by_uuid), None)
  if anchor is None:
    # the copy has not reached the remembered rows: it is still being written
    # while the file's newest record is one the family stores, and continues
    # nothing otherwise
    return _copy_in_progress() if probe.stored_by_family() else None
  anchor_line = line_by_uuid[anchor.uuid]
  if anchor_line + 1 == evidence.line_count:
    return _copy_in_progress()
  chunks: list[list[int]] = []
  if copy_start_line > 0:
    chunks.append([0, copy_start_line])
  chunks.append([anchor_line + 1, anchor_line + 1])
  return LineageDecision(
    forked_from={'trail_id': header['id'], 'step_id': anchor.step_id},
    chunks=chunks,
  )


def _extent(header: dict) -> int:
  extent: Any = header.get('extent')
  if not isinstance(extent, int) or isinstance(extent, bool) or extent < 0:
    raise ValueError(f'trail {header.get("id")!r} has no recorded extent')
  return extent
