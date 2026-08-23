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

Two continuations are verifiable:

- a same-segment resume starts after the recorded extent and forks from the
  prior trail's final row, verified by digest — every row the uuids matched,
  and the trail's final row at the line those matches place it, must hash to
  the file's line there;
- a new-segment resume forks from the matching uuid line in the parent and
  keeps only the new segment's own contribution (pre-copy ephemera plus the
  post-copy tail): the parent's first record uuid and one of its recent record
  uuids must both appear in the copy. Recent-tail rather than full-prefix
  equality, because the copy drops ephemeral records; hashes cannot serve here
  at all, since the rewritten `sessionId` changes every copied line's digest.

Claude writes a forked segment in stages — head ephemera, then the re-serialized
history, then the new turns — and a copy read mid-write verifies as no copy at
all, so a file whose newest record the chain already stores is not adopted yet.
`/clear` and failed verification both yield a root rather than an invented edge.
"""

import dataclasses
from typing import Any, Optional, Protocol

from bro.trails.lineage import LineageDecision, walk_header_chain

# how many of the parent trail's trailing record uuids may end a verified fork
# copy: the copy drops ephemeral records, so the very last uuids can be missing
# even when the history copy is intact
_VERIFY_TAIL_UUIDS = 20

# how far back a candidate probe reaches: wide enough to span what claude
# appends between a recorder's polls, so the parent's newest rows are still
# inside the window at the tick that adopts the transcript
_PROBE_TAIL_UUIDS = 64


class LineageIndex(Protocol):
  """The row reads a store offers a lineage resolver, always in process."""

  def get_trail(self, trail_id: str) -> dict: ...

  def find_segment_trails(self, segments: set[str], uuids: set[str]) -> list[dict]: ...

  def get_step_uuids(self, trail_id: str, *, through: Optional[int] = None) -> list[dict]: ...

  def step_payload_hashes(self, trail_id: str, step_ids: list[int]) -> dict[int, str]: ...


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


@dataclasses.dataclass(frozen=True)
class _ForkCuts:
  """Where the parent trail's history copy sits in a forked segment: the copy
  spans [copy_start_line, resume_start_line), and the new segment's own
  contribution is everything outside it. `anchor_index` is the parent step the
  fork points at — the last copied record found. `pending` marks a file whose
  newest record the chain already stores, so nothing of the segment's own is in
  it yet."""

  verified: bool
  pending: bool = False
  copy_start_line: int = 0
  resume_start_line: int = 0
  anchor_index: int = 0


def resolve(evidence: dict, index: LineageIndex) -> LineageDecision:
  """Decide the lineage of a trail about to record the transcript `evidence`
  describes."""
  parsed = _parse_evidence(evidence)
  if len(parsed.uuid_lines) == 0:
    return LineageDecision(adopt=False, reason='transcript carries no record yet')
  for header in _candidates(parsed, index):
    uuid_lines = [(row['step_id'], row['uuid']) for row in index.get_step_uuids(header['id'])]
    if header.get('native', {}).get('segment') == parsed.segment:
      decision = _continue_segment(header, uuid_lines, parsed, index)
    else:
      decision = _continue_copy(header, uuid_lines, parsed, index)
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
  recording its segment or a sibling and holding one of its records. Both
  continuations are anchored by the parent's newest rows — a same-segment parent
  ends where the resume begins, and a copy's chain records end where the new
  turns start — so the transcript's trailing uuids name the parent whenever one
  exists, and the rest are asked about only when they name nobody."""
  segments = {evidence.segment, *evidence.related}
  uuids = [uuid for _, uuid in evidence.uuid_lines]
  for probe in (uuids[-_PROBE_TAIL_UUIDS:], uuids[:-_PROBE_TAIL_UUIDS]):
    if len(probe) == 0:
      continue
    headers = index.find_segment_trails(segments, set(probe))
    if len(headers) == 0:
      continue
    for header in headers:
      if not isinstance(header.get('id'), str) or not isinstance(header.get('started_at'), str):
        raise ValueError(f'malformed lineage index trail: {header!r}')
    return sorted(headers, key=lambda header: header['started_at'], reverse=True)
  return []


def _continue_segment(
  header: dict, uuid_lines: list[tuple[int, str]], evidence: _Evidence, index: LineageIndex
) -> Optional[LineageDecision]:
  """Claude appended to the segment this trail recorded: the new trail starts
  past the recorded extent and forks from the prior trail's final row."""
  extent = _extent(header)
  if extent == 0:
    return None
  line_by_uuid = evidence.line_by_uuid()
  lines = {step_id: line_by_uuid[uuid] for step_id, uuid in uuid_lines if uuid in line_by_uuid}
  if len(lines) == 0:
    return None
  last_step = max(lines)
  # rows past the last matched one carry no uuid and follow it in the file: a
  # trail's stream is split only where it skipped a history copy, which is
  # always before its own first record
  last_line = lines[last_step] + (extent - 1 - last_step)
  if last_line >= evidence.line_count:
    return None
  lines[extent - 1] = last_line
  hashes = index.step_payload_hashes(header['id'], sorted(lines))
  # a matching uuid whose line hashes differently is a copy of that row, not
  # the row itself
  if any(hashes.get(step_id) != evidence.hashes[line] for step_id, line in lines.items()):
    return None
  if last_line + 1 == evidence.line_count:
    return LineageDecision(adopt=False, reason='no line past the recorded extent yet')
  return LineageDecision(
    forked_from={'trail_id': header['id'], 'step_id': extent - 1},
    chunks=[[last_line + 1, last_line + 1]],
  )


def _continue_copy(
  header: dict, uuid_lines: list[tuple[int, str]], evidence: _Evidence, index: LineageIndex
) -> Optional[LineageDecision]:
  """Claude forked a new segment re-serializing the history: verify the copy
  against the parent's uuids and keep only the new segment's own contribution —
  the recorded chain is authoritative for the copied part."""
  cuts = _fork_cuts(uuid_lines, _ancestor_uuids(header, index), evidence)
  if cuts.pending:
    return LineageDecision(adopt=False, reason='history copy still being written')
  if not cuts.verified:
    return None
  chunks: list[list[int]] = []
  if cuts.copy_start_line > 0:
    chunks.append([0, cuts.copy_start_line])
  chunks.append([cuts.resume_start_line, cuts.resume_start_line])
  return LineageDecision(
    forked_from={'trail_id': header['id'], 'step_id': cuts.anchor_index}, chunks=chunks
  )


def _fork_cuts(
  parent_uuid_lines: list[tuple[int, str]], ancestor_uuids: set[str], evidence: _Evidence
) -> _ForkCuts:
  """Locate the recorded chain's history copy in a forked segment and verify it
  is intact. `parent_uuid_lines` are the parent's (step index, uuid) pairs in
  step order; `ancestor_uuids` are the records the parent's own ancestors store —
  the copy re-serializes the whole conversation, so it starts at the earliest
  line either of them already holds."""
  if len(parent_uuid_lines) == 0:
    return _ForkCuts(verified=False)
  line_by_uuid = evidence.line_by_uuid()
  chain = ancestor_uuids | {uuid for _, uuid in parent_uuid_lines}
  pending = len(evidence.uuid_lines) == 0 or evidence.uuid_lines[-1][1] in chain
  parent_start = line_by_uuid.get(parent_uuid_lines[0][1])
  if parent_start is None:
    return _ForkCuts(verified=False, pending=pending)
  ancestor_lines = [line for uuid, line in line_by_uuid.items() if uuid in ancestor_uuids]
  copy_start = min([parent_start, *ancestor_lines])
  recent = {uuid for _, uuid in parent_uuid_lines[-_VERIFY_TAIL_UUIDS:]}
  for step_index, uuid in reversed(parent_uuid_lines):
    line = line_by_uuid.get(uuid)
    if line is not None:
      if uuid not in recent:
        return _ForkCuts(verified=False, pending=pending)
      return _ForkCuts(
        verified=True,
        pending=pending,
        copy_start_line=copy_start,
        resume_start_line=line + 1,
        anchor_index=step_index,
      )
  return _ForkCuts(verified=False, pending=pending)


def _ancestor_uuids(header: dict, index: LineageIndex) -> set[str]:
  """Every record uuid carried by the trail's bounded ancestor prefixes."""
  uuids: set[str] = set()
  for ancestor, bound in walk_header_chain(header, index.get_trail)[:-1]:
    assert bound is not None
    uuids.update(
      row['uuid'] for row in index.get_step_uuids(ancestor['id'], through=bound['step_id'])
    )
  return uuids


def _extent(header: dict) -> int:
  extent: Any = header.get('extent')
  if not isinstance(extent, int) or isinstance(extent, bool) or extent < 0:
    raise ValueError(f'trail {header.get("id")!r} has no recorded extent')
  return extent
