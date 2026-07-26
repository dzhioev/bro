#!/usr/bin/env python
"""record a Claude Code session's transcript to the trails service.

Claude Code writes one jsonl *segment* per session id under the project's
transcripts dir. An interactive leave→resume forks a fresh segment: a new
session id whose file re-serializes the conversation history — record `uuid`s
and `timestamp`s preserved, `sessionId` rewritten, ephemeral records dropped —
before the new turns. A headless resume appends to the existing segment.

One *trail* is the suffix recorded during one recorder lifetime within one
segment: the daemon creates a claude-harness trail when it adopts a transcript,
re-PUTs the complete current suffix on every change, and ends the trail on
segment transition or shutdown. Every process resume therefore opens a new
trail; a verified continuation is a fork:

- a same-segment resume starts after the previously recorded line extent and
  forks from the prior trail's final line, verified by boundary anchors — the
  stored artifact's first and last lines must match the segment file at the
  saved extent;
- a new-segment resume forks from the matching uuid line in the parent and
  stores only the new segment's own contribution (pre-copy ephemera plus the
  post-copy tail): the parent's first record uuid and one of its recent record
  uuids must both appear in the copy — recent-tail rather than full-prefix
  equality because claude's history copy is lossy. The copy is a
  re-serialization of the whole conversation, so its start is the earliest line
  the chain already holds, ancestors of the parent included;
- `/clear` or failed anchors starts a fresh root (no `forked_from`) rather than
  inventing lineage.

Claude writes a forked segment in stages — the head ephemera, then the
re-serialized history, then the new turns — and a copy read mid-write verifies
as no copy at all. The lineage decision is made once, on the tick that opens the
trail, so a segment is not adopted until it holds a record of its own: one whose
uuid the recorded chain does not already store.

The durable local state (`<config root>/recorder/<projects dir name>.json`)
maps the active segment to the trail id and the artifact's line ranges in the
segment file, saved after every successful snapshot so the recorded extent
always matches the server's artifact — that equality is what the next
lifetime's fork verification anchors on. It is a fast path, not the only
source: when it is absent or no longer matches what the server stores, the
parent is the newest trail recording the adopted segment (or the segment a
history copy came from), with its line range recovered by locating its stored
artifact in the local file. Without that lookup a transcript the store already
holds — every session recorded before this daemon existed — would be recorded
a second time as a fresh root.

Each snapshot refreshes the mutable header fields (`last_alive_at`,
`turn_count`, the native `harness_version` / `usage`) and conditionally
initializes `subject` from claude's generated `ai-title` while the server-side
subject is absent — an explicit rename wins and is never overwritten. Quiet
ticks keep the trail alive for the server's lost-sweep. The current trail id is
published to the session's trail pointer (`session_log/trail_pointer.py`) for
summon provenance.

The daemon is started by the in-place session runner (`cw/recorder.py`) next to
claude for every session flavor and finalizes on SIGTERM — one last snapshot,
then `end` with `ok`, or `raised` plus the reason when the transcript's
terminal record stream carries a bro `raise` service-tool call.
"""

import dataclasses
import datetime
import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

from base import configs, credentials, log
from base.args import Parser
from llm.tracker import HTTPStatusError
from session_log import health, trail_pointer
from trails.client import TrailsClient, default_client

__cli_name__ = 'session-log.recorder'

# how many of the parent trail's trailing record uuids may end a verified fork
# copy: the copy drops ephemeral records, so the very last uuids can be missing
# even when the history copy is intact
_VERIFY_TAIL_UUIDS = 20

# quiet ticks send a keepalive instead of a snapshot, throttled to this idle
# interval — well inside the server's lost-sweep threshold
_KEEPALIVE_INTERVAL_SECONDS = 60.0

# the bro service `raise` tool's wire name in a claude session's transcript
_RAISE_TOOL = 'mcp__bro__raise'


def _utc_now_iso() -> str:
  return datetime.datetime.now(datetime.UTC).isoformat()


def _read_lines(path: Path) -> list[str]:
  return path.read_text().splitlines()


def _parse_record(raw: str) -> Optional[dict]:
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return parsed if isinstance(parsed, dict) else None


def _state_path(projects_dir: Path) -> Path:
  """the recorder state file for a claude projects dir, under the config root
  (`<config>/recorder/`) so it survives resume relaunches in both session
  modes."""
  return projects_dir.parent.parent / 'recorder' / (projects_dir.name + '.json')


@dataclasses.dataclass
class RecorderState:
  """the durable per-projects-dir state: which segment the last trail recorded,
  which of the segment file's line ranges form its artifact (`chunks`, each
  `[start, end)` — at most a pre-copy head plus the tail), and the trail id.
  The final chunk's end is the consumed line extent as uploaded."""

  trail_id: str
  segment: str
  chunks: list[list[int]]

  @property
  def extent(self) -> int:
    return self.chunks[-1][1]

  @property
  def line_count(self) -> int:
    return sum(end - start for start, end in self.chunks)

  @classmethod
  def load(cls, path: Path) -> Optional['RecorderState']:
    try:
      data = json.loads(path.read_text())
    except FileNotFoundError:
      return None
    except (OSError, json.JSONDecodeError) as e:
      log.warning('unreadable recorder state %s (%s); starting a fresh root', path, e)
      return None
    try:
      chunks = [[int(start), int(end)] for start, end in data['chunks']]
      return cls(trail_id=data['trail_id'], segment=data['segment'], chunks=chunks)
    except (KeyError, TypeError, ValueError) as e:
      log.warning('malformed recorder state %s (%s); starting a fresh root', path, e)
      return None

  def save(self, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'trail_id': self.trail_id, 'segment': self.segment, 'chunks': self.chunks}
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


class _Scan:
  """metadata extraction over one trail's composed record stream: claude-code
  version, per-model raw usage sums, turn count, the generated title, and the
  terminal raise reason."""

  def __init__(self) -> None:
    self.harness_version: Optional[str] = None
    self.usage: dict[str, dict] = {}
    self.turn_count = 0
    self.ai_title: Optional[str] = None
    self.raised: Optional[str] = None
    # claude splits one API message across records, each repeating the
    # message's id and usage — sum a message id once or totals multiply
    self._billed_message_ids: set[str] = set()

  @staticmethod
  def _content_text(entry: dict) -> Optional[str]:
    content = entry.get('message', {}).get('content')
    if isinstance(content, str):
      return content
    if isinstance(content, list):
      for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
          return block.get('text')
    return None

  def feed(self, entry: dict) -> None:
    if self.harness_version is None:
      version = entry.get('version')
      if isinstance(version, str):
        self.harness_version = version

    if entry.get('type') == 'ai-title':
      title = entry.get('aiTitle')
      if isinstance(title, str) and len(title) > 0:
        self.ai_title = title

    if entry.get('type') == 'assistant':
      self._feed_assistant(entry)
    elif entry.get('type') == 'user':
      self._feed_user(entry)

  def _feed_assistant(self, entry: dict) -> None:
    message = entry.get('message')
    if not isinstance(message, dict):
      return
    usage = message.get('usage')
    model = str(message.get('model', 'unknown'))
    if (
      isinstance(usage, dict)
      and model != '<synthetic>'
      and entry.get('isApiErrorMessage') is not True
    ):
      message_id = message.get('id')
      if not isinstance(message_id, str) or message_id not in self._billed_message_ids:
        if isinstance(message_id, str):
          self._billed_message_ids.add(message_id)
        self.usage[model] = _add_numeric_maps(self.usage.get(model, {}), usage)
    content = message.get('content')
    if isinstance(content, list):
      for block in content:
        if (
          isinstance(block, dict)
          and block.get('type') == 'tool_use'
          and block.get('name') == _RAISE_TOOL
        ):
          reason = block.get('input', {}).get('reason')
          self.raised = reason if isinstance(reason, str) else ''

  def _feed_user(self, entry: dict) -> None:
    content = entry.get('message', {}).get('content')
    tool_results_only = isinstance(content, list) and all(
      isinstance(block, dict) and block.get('type') == 'tool_result' for block in content
    )
    if entry.get('isMeta') is not True and not tool_results_only:
      self.turn_count += 1
    # a real user message past a raise (a resume moving on) clears the abort
    if self.raised is not None and self._content_text(entry) is not None:
      self.raised = None


def _add_numeric_maps(left: dict, right: dict) -> dict:
  """sum `right`'s numeric leaves into `left`, recursing into nested maps;
  non-numeric values are ignored (raw claude usage mixes counters with
  service-tier strings)."""
  result = dict(left)
  for key, value in right.items():
    current = result.get(key)
    if isinstance(value, dict):
      result[key] = _add_numeric_maps(current if isinstance(current, dict) else {}, value)
    elif isinstance(value, int) and not isinstance(value, bool):
      result[key] = int(current) + value if isinstance(current, int) else value
  return result


def _scan_lines(lines: list[str]) -> _Scan:
  scan = _Scan()
  for raw in lines:
    entry = _parse_record(raw)
    if entry is not None:
      scan.feed(entry)
  return scan


def _uuid_lines(lines: list[str]) -> list[tuple[int, str]]:
  """(line index, uuid) for every record carrying a uuid, in order."""
  out: list[tuple[int, str]] = []
  for index, raw in enumerate(lines):
    entry = _parse_record(raw)
    if entry is None:
      continue
    uuid = entry.get('uuid')
    if isinstance(uuid, str):
      out.append((index, uuid))
  return out


@dataclasses.dataclass(frozen=True)
class _ForkCuts:
  """where the parent trail's history copy sits in a forked segment's file:
  the copy spans [copy_start_line, resume_start_line); the new segment's own
  contribution is everything outside it. `anchor_index` is the parent-artifact
  step index the fork points at — the last copied record found. `pending` marks
  a file whose newest record the chain already stores, so nothing of the
  segment's own is in it yet."""

  verified: bool
  pending: bool = False
  copy_start_line: int = 0
  resume_start_line: int = 0
  anchor_index: int = 0


def _fork_cuts(
  parent_uuid_lines: list[tuple[int, str]], ancestor_uuids: set[str], new_lines: list[str]
) -> _ForkCuts:
  """locate the recorded chain's history copy in a forked segment and verify it
  is intact: the parent's first uuid and one of its recent uuids must both
  appear in the new file. `parent_uuid_lines` are the parent artifact's
  (step index, uuid) pairs in step order; `ancestor_uuids` are the records the
  parent's own ancestors store — the copy re-serializes the whole conversation,
  so the copy starts at the earliest line either of them already holds."""
  if len(parent_uuid_lines) == 0:
    return _ForkCuts(verified=False)
  new_uuid_lines = _uuid_lines(new_lines)
  line_by_uuid: dict[str, int] = {}
  for index, uuid in new_uuid_lines:
    if uuid not in line_by_uuid:
      line_by_uuid[uuid] = index
  chain = ancestor_uuids | {uuid for _, uuid in parent_uuid_lines}
  pending = len(new_uuid_lines) == 0 or new_uuid_lines[-1][1] in chain
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


def _first_timestamp(lines: list[str]) -> Optional[str]:
  """the earliest record timestamp in a transcript — the lower bound of the
  window any trail recording it started in."""
  for raw in lines:
    entry = _parse_record(raw)
    timestamp = entry.get('timestamp') if entry is not None else None
    if isinstance(timestamp, str):
      return timestamp
  return None


def _locate(lines: list[str], block: list[str]) -> Optional[int]:
  """the index where `block` sits in `lines` as a contiguous run, or None."""
  if len(block) == 0:
    return None
  for start in range(len(lines) - len(block) + 1):
    if lines[start] == block[0] and lines[start : start + len(block)] == block:
      return start
  return None


def _modified_at(path: Path) -> float:
  try:
    return path.stat().st_mtime
  except FileNotFoundError:
    return 0.0


def _compose(file_lines: list[str], chunks: list[list[int]]) -> list[str]:
  lines: list[str] = []
  for start, end in chunks:
    lines.extend(file_lines[start:end])
  return lines


def _encode_lines(lines: list[str]) -> str:
  if len(lines) == 0:
    return ''
  return '\n'.join(lines) + '\n'


def _launch_context() -> Optional[Any]:
  raw = os.environ.get('CW_SESSION_CONTEXT')
  if raw is None:
    return None
  try:
    return json.loads(raw)
  except json.JSONDecodeError as e:
    log.warning('unparsable CW_SESSION_CONTEXT (%s); omitting the launch context', e)
    return None


def _in_container() -> bool:
  return os.path.isfile('/.dockerenv')


def _location(workspace: str) -> dict:
  in_container = _in_container()
  location: dict[str, Any] = {'workspace': workspace, 'is_container': in_container}
  # `host` is the host machine, never a container hostname: the launcher stamps
  # CW_HOST into the container env (workspace/docker.py); on host we are it
  host = os.environ.get('CW_HOST')
  if host is None and not in_container:
    host = socket.gethostname()
  if host is not None:
    location['host'] = host
  directory = os.environ.get('CW_HOST_WORKSPACE') if in_container else str(Path.cwd())
  if directory is not None:
    location['dir'] = directory
  return location


def _workspace_name() -> Optional[str]:
  return os.environ.get('CW_NAME')


@dataclasses.dataclass(frozen=True)
class _Continuation:
  """how an adopted transcript joins the recorded chain: the new trail's
  `forked_from` plus the segment line ranges it stores. `pending` instead marks
  a segment whose head claude has not finished writing, whose lineage is
  therefore not decidable yet."""

  forked_from: Optional[dict] = None
  chunks: list[list[int]] = dataclasses.field(default_factory=list)
  pending: bool = False


class Recorder:
  """tracks one workspace's active transcript and records it as fork trails.

  `started_after` gates segment adoption to files modified after the session
  launched, so a reused workspace's older transcripts are not re-recorded."""

  def __init__(
    self,
    projects_dir: Path,
    workspace: str,
    client: TrailsClient,
    *,
    llm: dict,
    cw_command: str,
    started_after: float,
  ) -> None:
    self.projects_dir = projects_dir
    self.workspace = workspace
    self.client = client
    self.llm = llm
    self.cw_command = cw_command
    self.started_after = started_after
    self.state_path = _state_path(projects_dir)
    self.previous: Optional[RecorderState] = RecorderState.load(self.state_path)
    self.active: Optional[RecorderState] = None
    self._known_subject: Optional[str] = None
    self._scan: Optional[_Scan] = None
    self._consumed: set[str] = set()
    self._uploaded_signature: Optional[tuple[str, int, int]] = None
    self._active_signature: Optional[tuple[str, int, int]] = None
    self._last_write_monotonic = time.monotonic()
    # a stale pointer from a previous lifetime must not attribute this
    # session's summons to an ended trail
    trail_pointer.clear()

  # --- tick loop -----------------------------------------------------------------

  def tick(self) -> bool:
    """one pass; True when a server write advanced the trail."""
    if self.active is None:
      return self._maybe_adopt()
    candidate = self._pick_segment()
    if candidate is not None and candidate.stem != self.active.segment:
      return self._maybe_transition(candidate)
    return self._snapshot_if_changed()

  def finalize(self) -> bool:
    """the daemon's shutdown pass: one last snapshot, then end the trail."""
    if self.active is None:
      self._maybe_adopt()
    if self.active is None:
      return False
    self._close_active()
    return True

  # --- segment selection ----------------------------------------------------------

  def _pick_segment(self) -> Optional[Path]:
    if not self.projects_dir.is_dir():
      return None
    active = self.active.segment if self.active is not None else None
    best: Optional[Path] = None
    best_mtime = 0.0
    for path in self.projects_dir.iterdir():
      if path.suffix != '.jsonl' or path.stem in self._consumed:
        continue
      try:
        mtime = path.stat().st_mtime
      except FileNotFoundError:
        continue
      if path.stem != active and mtime < self.started_after:
        continue
      if mtime > best_mtime:
        best = path
        best_mtime = mtime
    return best

  # --- adoption -------------------------------------------------------------------

  def _maybe_adopt(self) -> bool:
    path = self._pick_segment()
    if path is None:
      return False
    try:
      file_lines = _read_lines(path)
    except OSError:
      return False
    if len(_uuid_lines(file_lines)) == 0:
      # nothing but head ephemera: a history copy may still land, and the
      # segment's lineage is settled by the tick that adopts it
      return False
    continuation: Optional[_Continuation] = None
    for previous in self._parent_candidates(path.stem, file_lines):
      if previous.segment == path.stem:
        if len(file_lines) == previous.extent:
          return False  # no new content yet; claude's choice is not visible
        # only equality is quiet: a shorter file is a mismatch for the anchor
        # check below to reject
        continuation = self._same_segment_continuation(previous, file_lines)
      else:
        continuation = self._copied_history_continuation(previous, file_lines)
      if continuation is not None:
        break
    if continuation is not None and continuation.pending:
      return False
    forked_from = continuation.forked_from if continuation is not None else None
    chunks = continuation.chunks if continuation is not None else [[0, 0]]
    self._create_trail(path.stem, forked_from, chunks)
    self._snapshot_if_changed()
    return True

  def _parent_candidates(self, segment: str, file_lines: list[str]) -> Iterator[RecorderState]:
    """the lifetimes this transcript may continue, best first: the daemon's own
    durable state, then whatever the server already records for the segment —
    the adopted one, or the one a history copy was taken from."""
    if self.previous is not None:
      yield self.previous
    for candidate_segment, candidate_lines in self._recorded_segments(segment, file_lines):
      recorded = self._recorded_state(candidate_segment, candidate_lines)
      if recorded is not None:
        yield recorded

  def _recorded_segments(
    self, segment: str, file_lines: list[str]
  ) -> Iterator[tuple[str, list[str]]]:
    """the segments whose recorded trail could parent this transcript: the
    adopted segment itself, then the segment holding the first record of the
    history copy it opens with — claude rewrites `sessionId` in a copy, so a
    record uuid is the only link back to where the conversation was."""
    yield segment, file_lines
    uuids = _uuid_lines(file_lines)
    if len(uuids) == 0:
      return
    first_uuid = uuids[0][1]
    for path in sorted(self.projects_dir.glob('*.jsonl'), key=_modified_at, reverse=True):
      if path.stem == segment:
        continue
      try:
        lines = _read_lines(path)
      except OSError:
        continue
      if any(uuid == first_uuid for _, uuid in _uuid_lines(lines)):
        yield path.stem, lines
        return

  def _recorded_state(self, segment: str, file_lines: list[str]) -> Optional[RecorderState]:
    """the newest trail recording `segment`, with the line range its stored
    artifact occupies in that segment's file; None when no trail covers the
    segment or its artifact is not a contiguous slice of the file."""
    started_at = _first_timestamp(file_lines)
    if started_at is None:
      return None
    for header in self.client.iter_trails(harness='claude', since=started_at):
      if header.get('native', {}).get('segment') != segment:
        continue
      stored = self._stored_lines(header['id'])
      start = _locate(file_lines, stored) if stored is not None else None
      if stored is None or start is None:
        log.warning(
          'trail %s records segment %s but its artifact is not in the local file',
          header['id'],
          segment[:12],
        )
        return None
      return RecorderState(
        trail_id=header['id'], segment=segment, chunks=[[start, start + len(stored)]]
      )
    return None

  def _same_segment_continuation(
    self, previous: RecorderState, file_lines: list[str]
  ) -> Optional[_Continuation]:
    """claude appended to the recorded segment: the new trail records the lines
    past the saved extent and forks from the prior trail's final line — when
    the boundary anchors around that extent still hold."""
    if self._anchors_hold(previous, file_lines):
      forked_from = {'trail_id': previous.trail_id, 'step_id': str(previous.line_count - 1)}
      return _Continuation(forked_from=forked_from, chunks=[[previous.extent, previous.extent]])
    log.warning(
      'segment %s does not match trail %s at the recorded extent',
      previous.segment[:12],
      previous.trail_id,
    )
    return None

  def _anchors_hold(self, previous: RecorderState, file_lines: list[str]) -> bool:
    if previous.line_count == 0 or len(file_lines) < previous.extent:
      return False
    first = self._fetch_step_raw(previous.trail_id, '0', after=None)
    last = self._fetch_step_raw(
      previous.trail_id,
      str(previous.line_count - 1),
      after=str(previous.line_count - 2) if previous.line_count > 1 else None,
    )
    if first is None or last is None:
      return False
    return first == file_lines[previous.chunks[0][0]] and last == file_lines[previous.extent - 1]

  def _fetch_step_raw(self, trail_id: str, step_id: str, *, after: Optional[str]) -> Optional[str]:
    """one native record's raw line by step id, or None when the trail or step
    is definitively absent; transient server failures propagate so a blip
    retries next tick instead of degrading lineage to a root."""
    try:
      page = self.client.get_steps(trail_id, after=after, limit=1)
    except HTTPStatusError as exception:
      if exception.status == 404:
        return None
      raise
    for row in page.get('steps', []):
      if row.get('step_id') == step_id:
        return row.get('raw')
    return None

  def _copied_history_continuation(
    self, previous: RecorderState, file_lines: list[str]
  ) -> Optional[_Continuation]:
    """claude forked a new segment re-serializing the history: verify the copy
    against the parent trail's uuids and store only the new segment's own
    contribution — the recorded chain is authoritative for the copied part."""
    parent_uuid_lines = self._parent_uuid_lines(previous)
    if parent_uuid_lines is None:
      return None
    cuts = _fork_cuts(parent_uuid_lines, self._ancestor_uuids(previous.trail_id), file_lines)
    if cuts.pending:
      log.info('segment holds no record past the copy of trail %s yet', previous.trail_id)
      return _Continuation(pending=True)
    if not cuts.verified:
      log.info('segment does not carry a verified copy of trail %s', previous.trail_id)
      return None
    forked_from = {'trail_id': previous.trail_id, 'step_id': str(cuts.anchor_index)}
    chunks: list[list[int]] = []
    if cuts.copy_start_line > 0:
      chunks.append([0, cuts.copy_start_line])
    chunks.append([cuts.resume_start_line, cuts.resume_start_line])
    return _Continuation(forked_from=forked_from, chunks=chunks)

  def _parent_uuid_lines(self, previous: RecorderState) -> Optional[list[tuple[int, str]]]:
    """the parent trail's (step index, uuid) pairs in step order — from its
    local segment file when that composition is still the artifact the server
    holds (the fast path), else from the server's step stream; None when the
    trail is definitively absent."""
    header = self._header(previous.trail_id)
    if header is None:
      return None
    if previous.line_count == header.get('native', {}).get('line_count'):
      try:
        file_lines = _read_lines(self.projects_dir / (previous.segment + '.jsonl'))
      except OSError:
        file_lines = None
      if file_lines is not None and len(file_lines) >= previous.extent:
        return _uuid_lines(_compose(file_lines, previous.chunks))
    stored = self._stored_lines(previous.trail_id)
    return None if stored is None else _uuid_lines(stored)

  def _ancestor_uuids(self, trail_id: str) -> set[str]:
    """every record uuid the trail's own ancestors store. Claude's history copy
    re-serializes the whole conversation, so without them a chain's earlier
    lifetimes land in the child's pre-copy head and are stored twice."""
    uuids: set[str] = set()
    walked = {trail_id}
    ancestor = self._forked_from(trail_id)
    while ancestor is not None:
      if ancestor in walked:
        raise ValueError(f'fork chain of trail {trail_id} cycles through {ancestor}')
      walked.add(ancestor)
      stored = self._stored_lines(ancestor)
      if stored is None:
        return uuids
      uuids.update(uuid for _, uuid in _uuid_lines(stored))
      ancestor = self._forked_from(ancestor)
    return uuids

  def _forked_from(self, trail_id: str) -> Optional[str]:
    header = self._header(trail_id)
    forked_from = header.get('forked_from') if header is not None else None
    return forked_from['trail_id'] if isinstance(forked_from, dict) else None

  def _header(self, trail_id: str) -> Optional[dict]:
    """the trail's header, or None when the server does not know it."""
    try:
      return self.client.get_trail(trail_id)
    except HTTPStatusError as exception:
      if exception.status == 404:
        return None
      raise

  def _stored_lines(self, trail_id: str) -> Optional[list[str]]:
    """the raw native records of a trail's stored artifact, in step order."""
    try:
      return [row['raw'] for row in self.client.iter_steps(trail_id)]
    except HTTPStatusError as exception:
      if exception.status == 404:
        return None
      raise

  # --- trail lifecycle ------------------------------------------------------------

  def _create_trail(
    self, segment: str, forked_from: Optional[dict], chunks: list[list[int]]
  ) -> None:
    payload: dict[str, Any] = {
      'harness': 'claude',
      'version': configs.VERSION,
      'interactive': True,
      'surface': 'cw',
      'native': {
        'llm': self.llm,
        'segment': segment,
        'cw_command': self.cw_command,
        'harness_version': 'unknown',  # refreshed from the records per snapshot
      },
      'location': _location(self.workspace),
      'body': {'artifact': ''},
    }
    bro = os.environ.get('CW_BRO')
    if bro is not None:
      payload['bro'] = bro
    hold = os.environ.get('BRO_HOLD')
    if hold is not None:
      payload['hold'] = hold
    if forked_from is not None:
      payload['forked_from'] = forked_from
    context = _launch_context()
    if context is not None:
      payload['body']['launch_context'] = context
    trail_id = self.client.create_trail(payload)['id']
    self.active = RecorderState(trail_id=trail_id, segment=segment, chunks=chunks)
    self._known_subject = None
    self._scan = None
    self._uploaded_signature = None
    self._last_write_monotonic = time.monotonic()
    self.active.save(self.state_path)
    trail_pointer.publish(trail_id)
    if forked_from is None:
      log.info('trail %s opens at segment %s (root)', trail_id, segment[:12])
    else:
      log.info(
        'trail %s opens at segment %s (forked from %s @ %s)',
        trail_id,
        segment[:12],
        forked_from['trail_id'],
        forked_from['step_id'],
      )

  def _active_path(self) -> Path:
    assert self.active is not None
    return self.projects_dir / (self.active.segment + '.jsonl')

  def _snapshot_if_changed(self) -> bool:
    active = self.active
    assert active is not None
    path = self._active_path()
    try:
      stat = path.stat()
    except FileNotFoundError:
      self._keepalive_if_idle()
      return False
    signature = (active.segment, stat.st_mtime_ns, stat.st_size)
    self._active_signature = signature
    if signature == self._uploaded_signature:
      self._keepalive_if_idle()
      return False
    file_lines = _read_lines(path)
    if len(file_lines) < active.chunks[-1][0]:
      # the segment file shrank below the trail's tail start (a transcript
      # rewrite): close here and let the next tick re-adopt what remains
      log.warning('segment %s shrank below trail %s; closing', active.segment[:12], active.trail_id)
      self._close_active()
      return True
    self._snapshot(file_lines)
    self._uploaded_signature = signature
    return True

  def _snapshot(self, file_lines: list[str]) -> None:
    active = self.active
    assert active is not None
    active.chunks[-1][1] = max(active.chunks[-1][0], len(file_lines))
    lines = _compose(file_lines, active.chunks)
    scan = _scan_lines(lines)
    native: dict[str, Any] = {'usage': scan.usage}
    if scan.harness_version is not None:
      native['harness_version'] = scan.harness_version
    self.client.replace_artifact(active.trail_id, _encode_lines(lines), native)
    changes: dict[str, Any] = {
      'last_alive_at': _utc_now_iso(),
      'turn_count': scan.turn_count,
    }
    if self._known_subject is None and scan.ai_title is not None:
      changes['subject'] = scan.ai_title
    header = self.client.update_header(active.trail_id, changes)
    self._known_subject = header.get('subject')
    self._scan = scan
    self._last_write_monotonic = time.monotonic()
    active.save(self.state_path)
    log.info(
      'recorded trail %s (%d lines, segment %s)',
      active.trail_id,
      len(lines),
      active.segment[:12],
    )

  def _keepalive_if_idle(self) -> None:
    if self.active is None:
      return
    if time.monotonic() - self._last_write_monotonic < _KEEPALIVE_INTERVAL_SECONDS:
      return
    self.client.keepalive(self.active.trail_id)
    self._last_write_monotonic = time.monotonic()

  def _maybe_transition(self, new_path: Path) -> bool:
    active = self.active
    assert active is not None
    if not self._active_is_quiet():
      # a live active segment plus an unrelated newer jsonl: hold rather than
      # flip recording back and forth between two growing files
      log.warning(
        'segment %s appeared while %s is still growing; holding',
        new_path.stem[:12],
        active.segment[:12],
      )
      return self._snapshot_if_changed()
    self._close_active()
    self._maybe_adopt()
    return True

  def _active_is_quiet(self) -> bool:
    try:
      stat = self._active_path().stat()
    except FileNotFoundError:
      return True
    if stat.st_mtime < self.started_after:
      return True
    assert self.active is not None
    signature = (self.active.segment, stat.st_mtime_ns, stat.st_size)
    return signature == self._active_signature

  def _close_active(self) -> None:
    """final snapshot, then end the trail — `ok`, or `raised` with the reason
    when the recorded stream's terminal state is a raise call. the closed state
    stays on disk as the parent for the next lifetime's fork verification."""
    active = self.active
    assert active is not None
    try:
      file_lines = _read_lines(self._active_path())
    except OSError:
      file_lines = None
    if file_lines is not None and len(file_lines) >= active.chunks[-1][0]:
      self._snapshot(file_lines)
    raised = self._scan.raised if self._scan is not None else None
    if raised is not None:
      detail = raised if len(raised) > 0 else 'raise reason unavailable'
      self.client.end_trail(active.trail_id, 'raised', detail=detail)
    else:
      self.client.end_trail(active.trail_id, 'ok')
    log.info('trail %s ended (%s)', active.trail_id, 'raised' if raised is not None else 'ok')
    self._consumed.add(active.segment)
    self.previous = active
    self.active = None
    self._scan = None
    self._uploaded_signature = None
    trail_pointer.clear()


def _exception_summary(exception: BaseException) -> str:
  return f'{type(exception).__name__}: {exception}'


def _attempt(step: Callable[[], bool]) -> None:
  try:
    advanced = step()
  except Exception as e:
    log.exception('recording failed')
    health.write('error', _exception_summary(e))
    return
  if advanced:
    health.write('ok')


def _watch(recorder: Recorder, interval: int) -> None:
  stop = threading.Event()
  parent_pid = os.getppid()

  def _handle_signal(signum, frame):
    del signum, frame
    stop.set()

  signal.signal(signal.SIGTERM, _handle_signal)
  signal.signal(signal.SIGINT, _handle_signal)

  while not stop.is_set():
    if os.getppid() != parent_pid:
      log.info('parent process exited, shutting down')
      break
    _attempt(recorder.tick)
    stop.wait(interval)

  _attempt(recorder.finalize)


def _projects_dir_from_environment() -> Path:
  """a host cw session points CLAUDE_CONFIG_DIR at its private per-session
  state dir (reference/cw.md, "Host claude-state isolation"); its transcripts
  live under that dir's projects/, not the host ~/.claude's."""
  config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
  claude_dir = Path(config_dir) if config_dir is not None else Path.home() / '.claude'
  projects_root = claude_dir / 'projects'
  pwd = os.environ.get('PWD')
  cwd = Path(pwd if pwd is not None else os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = projects_root / str(candidate).replace('/', '-').replace('.', '-')
    if project_dir.is_dir():
      return project_dir
  return projects_root / str(cwd).replace('/', '-').replace('.', '-')


def record_session(
  interval: int = 15,
  workspace: Optional[str] = None,
  projects_dir: Optional[Path] = None,
  llm: Optional[str] = None,
) -> int:
  workspace_name = workspace if workspace is not None else _workspace_name()
  if workspace_name is None:
    log.error('cannot determine workspace name; pass --workspace or set CW_NAME')
    return 1
  cw_command = os.environ.get('CW_COMMAND')
  if cw_command is None:
    log.error('CW_COMMAND is not set; the trail header requires the launch command')
    return 1
  try:
    llm_recipe = json.loads(llm) if llm is not None else {}
  except json.JSONDecodeError as e:
    log.error('invalid --llm json: %s', e)
    return 1

  try:
    client = default_client()
  except credentials.SecretNotFound:
    log.error('config not found: trails (run trails/bootstrap.sh)')
    health.write('error', 'config not found: trails')
    return 1

  src = projects_dir if projects_dir is not None else _projects_dir_from_environment()
  recorder = Recorder(
    src,
    workspace_name,
    client,
    llm=llm_recipe,
    cw_command=cw_command,
    started_after=time.time(),
  )
  log.info('recording %s (interval=%ds, workspace=%s)', src, interval, workspace_name)
  _watch(recorder, interval)
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='record a Claude Code session transcript to trails')
  parser.add_argument(
    '--interval', type=int, default=15, help='poll interval in seconds (default: 15)'
  )
  parser.add_argument('--workspace', default=None, help='workspace name (default: from CW_NAME)')
  parser.add_argument(
    '--projects-dir',
    type=Path,
    default=None,
    help='claude projects dir to record (default: derived from the config dir and cwd)',
  )
  parser.add_argument(
    '--llm',
    default=None,
    help='launch recipe json for native.llm, e.g. {"model": "...", "effort": "..."}',
  )
  return record_session(**parser.parse(argv))
