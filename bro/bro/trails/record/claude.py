#!/usr/bin/env python
"""sync Claude Code session logs to S3 + DynamoDB, one conversation per item.

Claude Code writes one jsonl *segment* per session id under the project's
transcripts dir. An interactive leave→resume forks a fresh segment: a new
session id whose file re-serializes the conversation history — record `uuid`s
and `timestamp`s preserved, `sessionId` rewritten, ephemeral records (queue
operations, mode changes, titles) dropped — before the new turns. A headless
resume appends to the existing segment instead.

This tool stitches segments into *conversations*. A conversation is keyed by a
minted lulid and tracked as a *timeline* in a state file under the session's
claude config dir (`conversations/<encoded workspace>.json`): an ordered list
of segment-file line ranges (chunks) interleaved with `cw-conversation-event`
records (leave / resume markers). The uploaded artifact is the timeline's
composition — each chunk read from its segment's original file, so the stored
log keeps the original `sessionId`s and ephemeral records rather than the
fork's rewrite, and only the fork's duplicated history copy is skipped.

The timeline's stable head — everything strictly before its last chunk, which
is the only part later mutations can touch — is cached composed on disk next
to the state file (`….prefix.jsonl`) and advanced at transitions. An upload is
therefore the cached prefix plus the live tail composed fresh from the active
segment's file: old segments are read once, at the transition that freezes
them, and need not exist afterwards. The cache is derived state — integrity is
checked against the recorded byte count on every read (a torn append is
truncated away, anything else rebuilds from the timeline), and a rebuild whose
source file is gone degrades to an explicit missing-segment marker record.

A forked segment joins the conversation only when its history copy verifies —
the previous segment's first and one of its recent record uuids both appear in
it (the same lookup locates the copy's line range to skip); an unverified fork
(`/clear`, or a claude whose resume stops copying history) starts a new
conversation instead, so an uploaded log never silently loses content.

One-shot by default; `--watch` runs the continuous daemon the in-place session
runner (cw/runner.py) starts next to claude for every session flavor. The
daemon polls, uploads on change, and finalizes on SIGTERM — appending a leave
event and uploading one last time.
"""

import dataclasses
import datetime
import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import boto3

import session_log_health
from base import credentials, log
from base.args import Parser
from base.lulid import lulid

__cli_name__ = 'sync-session-log'

EVENT_TYPE = 'cw-conversation-event'

# how many of the previous segment's trailing record uuids may end a verified
# fork copy: the copy drops ephemeral records, so the very last uuids can be
# missing even when the history copy is intact
_VERIFY_TAIL_UUIDS = 20


def _load_config() -> dict:
  return credentials.get_json('session_log')


def _create_session(config: dict) -> boto3.Session:
  return boto3.Session(
    aws_access_key_id=config['aws_access_key_id'],
    aws_secret_access_key=config['aws_secret_access_key'],
    region_name=config['region'],
  )


class _Store:
  """the S3 + DynamoDB pair a conversation uploads to."""

  def __init__(self, session: boto3.Session, bucket: str, table: str) -> None:
    self.bucket = bucket
    self.table = table
    self.s3 = session.client('s3')
    self.dynamo = session.client('dynamodb')

  def put_log(self, key: str, body: bytes) -> None:
    self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)

  def put_item(self, item: dict) -> None:
    self.dynamo.put_item(TableName=self.table, Item={k: _to_attribute(v) for k, v in item.items()})


def _encode_cwd(cwd: str) -> str:
  return cwd.replace('/', '-').replace('.', '-')


def _projects_dir() -> Path:
  # a host cw session points CLAUDE_CONFIG_DIR at its private per-session state
  # dir (reference/cw.md, "Host claude-state isolation"); its transcripts live
  # under that dir's projects/, not the host ~/.claude's
  config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
  claude_dir = Path(config_dir) if config_dir is not None else Path.home() / '.claude'
  projects_root = claude_dir / 'projects'
  pwd = os.environ.get('PWD')
  cwd = Path(pwd if pwd is not None else os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = projects_root / _encode_cwd(str(candidate))
    if project_dir.is_dir():
      return project_dir
  return projects_root / _encode_cwd(str(cwd))


def _conversation_state_path(projects_dir: Path) -> Path:
  """the conversation state file for a claude projects dir, under the config
  root (`<config>/conversations/`) so it survives resume relaunches in both
  session modes."""
  return projects_dir.parent.parent / 'conversations' / (projects_dir.name + '.json')


def _conversation_prefix_path(projects_dir: Path) -> Path:
  """the frozen-prefix cache next to the conversation state file."""
  return projects_dir.parent.parent / 'conversations' / (projects_dir.name + '.prefix.jsonl')


@dataclasses.dataclass
class ConversationEvent:
  """a leave / resume marker on the conversation timeline, emitted into the
  uploaded jsonl at its timeline position."""

  subtype: str  # 'leave' | 'resume'
  timestamp: str
  session_id: str
  previous_session_id: Optional[str] = None
  previous_conversation_id: Optional[str] = None
  verified: Optional[bool] = None

  def to_record(self) -> dict:
    record: dict = {
      'type': EVENT_TYPE,
      'subtype': self.subtype,
      'timestamp': self.timestamp,
      'sessionId': self.session_id,
    }
    if self.previous_session_id is not None:
      record['previousSessionId'] = self.previous_session_id
    if self.previous_conversation_id is not None:
      record['previousConversationId'] = self.previous_conversation_id
    if self.verified is not None:
      record['historyVerified'] = self.verified
    return record


@dataclasses.dataclass
class Chunk:
  """a line range of one segment's file on the conversation timeline.

  `end_line` (exclusive) None means the chunk is open — it follows the file to
  its current end; only the timeline's last chunk may be open. A fork's
  duplicated history copy is the part *between* the chunks of consecutive
  segments, which is why concatenating the chunks yields the conversation
  without duplication."""

  segment: str
  start_line: int
  end_line: Optional[int] = None


@dataclasses.dataclass
class CachedPrefix:
  """bookkeeping for the on-disk frozen-prefix cache: how many timeline items
  it covers, its exact byte / line extent, and the metadata-scan snapshot over
  its records (so an upload's scan resumes from here instead of re-parsing)."""

  items: int = 0
  byte_count: int = 0
  line_count: int = 0
  scan: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ConversationState:
  conversation_id: str
  timeline: list[Chunk | ConversationEvent]
  prefix: CachedPrefix = dataclasses.field(default_factory=CachedPrefix)

  def _chunks(self) -> list[Chunk]:
    return [item for item in self.timeline if isinstance(item, Chunk)]

  @property
  def segments(self) -> list[str]:
    """the claude session ids the conversation spans, in first-appearance order."""
    out: list[str] = []
    for chunk in self._chunks():
      if chunk.segment not in out:
        out.append(chunk.segment)
    return out

  @property
  def active_segment(self) -> str:
    return self._chunks()[-1].segment

  def last_chunk(self) -> Chunk:
    return self._chunks()[-1]

  @classmethod
  def load(cls, path: Path) -> Optional['ConversationState']:
    try:
      data = json.loads(path.read_text())
    except FileNotFoundError:
      return None
    except (OSError, json.JSONDecodeError) as e:
      log.warning('unreadable conversation state %s (%s); starting a new conversation', path, e)
      return None
    try:
      return cls(
        conversation_id=data['conversation_id'],
        timeline=[_timeline_item_from_json(item) for item in data['timeline']],
        prefix=CachedPrefix(**data.get('prefix', {})),
      )
    except (KeyError, TypeError, ValueError) as e:
      log.warning('malformed conversation state %s (%s); starting a new conversation', path, e)
      return None

  def save(self, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
      'conversation_id': self.conversation_id,
      'timeline': [_timeline_item_to_json(item) for item in self.timeline],
      'prefix': dataclasses.asdict(self.prefix),
    }
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _timeline_item_to_json(item: Chunk | ConversationEvent) -> dict:
  if isinstance(item, Chunk):
    return {'kind': 'chunk', **dataclasses.asdict(item)}
  return {'kind': 'event', **dataclasses.asdict(item)}


def _timeline_item_from_json(data: dict) -> Chunk | ConversationEvent:
  kind = data['kind']
  fields = {k: v for k, v in data.items() if k != 'kind'}
  if kind == 'chunk':
    return Chunk(**fields)
  if kind == 'event':
    return ConversationEvent(**fields)
  raise ValueError(f'unknown timeline item kind {kind!r}')


def _read_lines(path: Path) -> list[str]:
  return path.read_text().splitlines()


def _parse_record(raw: str) -> Optional[dict]:
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return parsed if isinstance(parsed, dict) else None


@dataclasses.dataclass
class _SegmentScan:
  """one pass over a segment file: its raw line count and ordered record uuids."""

  line_count: int
  uuids: list[str]


def _scan_segment(path: Path) -> _SegmentScan:
  uuids: list[str] = []
  lines = _read_lines(path)
  for raw in lines:
    entry = _parse_record(raw)
    if entry is None:
      continue
    uuid = entry.get('uuid')
    if isinstance(uuid, str):
      uuids.append(uuid)
  return _SegmentScan(line_count=len(lines), uuids=uuids)


@dataclasses.dataclass
class _ForkCuts:
  """where a forked segment's duplicated history copy sits in its file.

  `copy_start_line` is the first copied record's line; `resume_start_line` is
  the line after the last copied record found — the fork's own content is
  [0, copy_start_line) (pre-copy ephemera) plus [resume_start_line, EOF)."""

  verified: bool
  copy_start_line: int = 0
  resume_start_line: int = 0
  old_line_count: int = 0


def _fork_cuts(old_path: Path, new_path: Path) -> _ForkCuts:
  """locate the history copy a forked segment carries and verify it is intact:
  the old segment's first record uuid and one of its recent uuids must both
  appear in the new file."""
  try:
    old = _scan_segment(old_path)
    new_lines = _read_lines(new_path)
  except OSError:
    return _ForkCuts(verified=False)
  if len(old.uuids) == 0:
    return _ForkCuts(verified=False)

  line_by_uuid: dict[str, int] = {}
  for index, raw in enumerate(new_lines):
    entry = _parse_record(raw)
    if entry is None:
      continue
    uuid = entry.get('uuid')
    if isinstance(uuid, str) and uuid not in line_by_uuid:
      line_by_uuid[uuid] = index

  copy_start = line_by_uuid.get(old.uuids[0])
  anchor_line: Optional[int] = None
  anchor_recent = False
  recent = set(old.uuids[-_VERIFY_TAIL_UUIDS:])
  for uuid in reversed(old.uuids):
    line = line_by_uuid.get(uuid)
    if line is not None:
      anchor_line = line
      anchor_recent = uuid in recent
      break
  if copy_start is None or anchor_line is None or not anchor_recent:
    return _ForkCuts(verified=False)
  return _ForkCuts(
    verified=True,
    copy_start_line=copy_start,
    resume_start_line=anchor_line + 1,
    old_line_count=old.line_count,
  )


class _MetadataScan:
  """subject / model / version / start-time extraction over the record stream,
  fed one parsed record at a time while the artifact is composed. Snapshots
  let the scan over the cached prefix persist in the state file, so an upload
  resumes it over the live tail only."""

  def __init__(self) -> None:
    self.subject: Optional[str] = None
    self.model: Optional[str] = None
    self.version: Optional[str] = None
    self.started_at: Optional[str] = None

  def to_snapshot(self) -> dict:
    return {
      'subject': self.subject,
      'model': self.model,
      'version': self.version,
      'started_at': self.started_at,
    }

  @classmethod
  def from_snapshot(cls, snapshot: dict) -> '_MetadataScan':
    scan = cls()
    scan.subject = snapshot.get('subject')
    scan.model = snapshot.get('model')
    scan.version = snapshot.get('version')
    scan.started_at = snapshot.get('started_at')
    return scan

  def feed(self, entry: dict) -> None:
    if self.started_at is None:
      timestamp = entry.get('timestamp')
      if isinstance(timestamp, str):
        self.started_at = timestamp

    if (
      self.subject is None and entry.get('type') == 'user' and entry.get('isSidechain') is not True
    ):
      content = entry.get('message', {}).get('content')
      text: Optional[str] = None
      if isinstance(content, str):
        text = content
      elif isinstance(content, list):
        for c in content:
          if isinstance(c, dict) and c.get('type') == 'text':
            text = c.get('text')
            break
      if text is not None:
        stripped = text.lstrip()
        if not stripped.startswith('<'):
          first_line = stripped.split('\n', 1)[0].strip()
          if len(first_line) > 0:
            self.subject = first_line

    msg = entry.get('message')
    if isinstance(msg, dict) and 'model' in msg:
      self.model = msg['model']

    if self.version is None:
      version = entry.get('version')
      if isinstance(version, str):
        self.version = version


@dataclasses.dataclass
class _Composed:
  body: bytes
  scan: _MetadataScan
  line_count: int


def _missing_segment_record(segment: str) -> dict:
  return {
    'type': EVENT_TYPE,
    'subtype': 'missing-segment',
    'timestamp': _utc_now_iso(),
    'sessionId': segment,
  }


def _compose_items(
  projects_dir: Path, items: list[Chunk | ConversationEvent], scan: _MetadataScan
) -> list[str]:
  """compose timeline items into raw jsonl lines: chunks read from their
  segments' original files, events emitted at their positions. a chunk whose
  file is gone degrades to a missing-segment marker. events are not fed to the
  metadata scan, so `started_at` stays the conversation's first content
  timestamp."""
  lines: list[str] = []
  for item in items:
    if isinstance(item, ConversationEvent):
      lines.append(json.dumps(item.to_record()))
      continue
    path = projects_dir / (item.segment + '.jsonl')
    try:
      file_lines = _read_lines(path)
    except OSError:
      log.warning('segment %s file missing; emitting a marker', item.segment[:12])
      lines.append(json.dumps(_missing_segment_record(item.segment)))
      continue
    end = item.end_line if item.end_line is not None else len(file_lines)
    for raw in file_lines[item.start_line : end]:
      lines.append(raw)
      entry = _parse_record(raw)
      if entry is not None:
        scan.feed(entry)
  return lines


def _encode_lines(lines: list[str]) -> bytes:
  if len(lines) == 0:
    return b''
  return ('\n'.join(lines) + '\n').encode()


def _to_attribute(value: str | int | bool) -> dict:
  if isinstance(value, bool):
    return {'BOOL': value}
  if isinstance(value, int):
    return {'N': str(value)}
  return {'S': str(value)}


def _build_item(state: ConversationState, workspace: str, s3_key: str, composed: _Composed) -> dict:
  item: dict = {
    'session_id': state.conversation_id,
    'workspace': workspace,
    'host': socket.gethostname(),
    's3_key': s3_key,
    'size_bytes': len(composed.body),
    'line_count': composed.line_count,
    'synced_at': _utc_now_iso(),
    'is_container': os.path.isfile('/.dockerenv'),
    'segments': json.dumps(state.segments),
  }

  for key, env in [
    ('cw_command', 'CW_COMMAND'),
    ('shell_command', 'PPP_SHELL_COMMAND'),
    # launch-context records captured by cw (cw/session_context.py); a JSON list
    # of typed records rendered by rewind as a SESSION CONTEXT preamble
    ('context', 'CW_SESSION_CONTEXT'),
  ]:
    value = os.environ.get(env)
    if value is not None:
      item[key] = value

  scan = composed.scan
  for attribute, value in (
    ('subject', scan.subject),
    ('model', scan.model),
    ('claude_code_version', scan.version),
    ('started_at', scan.started_at),
  ):
    if value is not None:
      item[attribute] = value

  return item


def _utc_now_iso() -> str:
  return datetime.datetime.now(datetime.UTC).isoformat()


def _mtime_iso(path: Path) -> str:
  try:
    mtime = path.stat().st_mtime
  except OSError:
    return _utc_now_iso()
  return datetime.datetime.fromtimestamp(mtime, datetime.UTC).isoformat()


def _workspace_name() -> Optional[str]:
  name = os.environ.get('CW_NAME')
  if name is not None:
    return name
  cw_command = os.environ.get('CW_COMMAND')
  if cw_command is None:
    return None
  parts = cw_command.split()
  if len(parts) < 3 or parts[0] != 'cw' or parts[1] != 'ss':
    return None
  i = 2
  while i < len(parts):
    arg = parts[i]
    if not arg.startswith('-'):
      return arg
    if '=' in arg:
      i += 1
      continue
    i += 1
  return None


def _exception_summary(exception: BaseException) -> str:
  return f'{type(exception).__name__}: {exception}'


def _line_count(path: Path) -> int:
  try:
    return len(_read_lines(path))
  except OSError:
    return 0


class ConversationSync:
  """tracks one workspace's active conversation and uploads it on change.

  `started_after` gates fresh-segment adoption to files modified after the
  session launched, so a reused workspace's older transcripts are not adopted
  into a new conversation; None (one-shot) adopts the newest file whatever its
  age. `resume_segment` names the claude session the launch resumes — it only
  matters without conversation state (a resume of a session synced before its
  conversation was tracked), seeding the timeline with that segment's original
  records when the fork verifies."""

  def __init__(
    self,
    projects_dir: Path,
    workspace: str,
    store: _Store,
    started_after: Optional[float],
    resume_segment: Optional[str] = None,
  ) -> None:
    self.projects_dir = projects_dir
    self.workspace = workspace
    self.store = store
    self.started_after = started_after
    self.resume_segment = resume_segment
    self.state_path = _conversation_state_path(projects_dir)
    self.prefix_path = _conversation_prefix_path(projects_dir)
    self.state: Optional[ConversationState] = ConversationState.load(self.state_path)
    self._uploaded_signature: Optional[tuple[str, int, int]] = None
    self._active_signature: Optional[tuple[str, int, int]] = None

  def tick(self) -> bool:
    """one sync pass; True when an upload happened."""
    if not self._refresh():
      return False
    return self._upload_if_changed()

  def finalize(self) -> bool:
    """the daemon's shutdown pass: freeze the timeline, record the leave, and
    upload one last time."""
    if not self._refresh():
      return False
    state = self.state
    assert state is not None
    self._freeze_active(_line_count(self._active_path()))
    self._ensure_leave(timestamp=_utc_now_iso())
    state.save(self.state_path)
    self._uploaded_signature = None
    return self._upload_if_changed()

  def _active_path(self) -> Path:
    assert self.state is not None
    return self.projects_dir / (self.state.active_segment + '.jsonl')

  def _refresh(self) -> bool:
    """adopt / transition to the current segment; True when a conversation is
    being tracked afterwards."""
    candidate = self._pick_segment()
    if candidate is None:
      return self.state is not None
    if self.state is None:
      self._adopt(candidate)
    elif candidate.stem != self.state.active_segment:
      self._maybe_transition(candidate)
    return self.state is not None

  def _pick_segment(self) -> Optional[Path]:
    if not self.projects_dir.is_dir():
      return None
    if self.state is not None:
      active = self.state.active_segment
      consumed = {segment for segment in self.state.segments if segment != active}
    else:
      active = None
      consumed = set()
    best: Optional[Path] = None
    best_mtime = 0.0
    for path in self.projects_dir.iterdir():
      if path.suffix != '.jsonl' or path.stem in consumed:
        continue
      try:
        mtime = path.stat().st_mtime
      except FileNotFoundError:
        continue
      if path.stem != active and self.started_after is not None and mtime < self.started_after:
        continue
      if mtime > best_mtime:
        best = path
        best_mtime = mtime
    return best

  def _adopt(self, path: Path) -> None:
    timeline: list[Chunk | ConversationEvent] = []
    if self.resume_segment is not None and self.resume_segment != path.stem:
      old_path = self.projects_dir / (self.resume_segment + '.jsonl')
      cuts = _fork_cuts(old_path, path)
      if cuts.verified:
        # the pre-tracking segment's original records open the conversation
        timeline.append(Chunk(self.resume_segment, 0, cuts.old_line_count))
        timeline.append(
          ConversationEvent(
            subtype='leave', timestamp=_mtime_iso(old_path), session_id=self.resume_segment
          )
        )
      timeline.append(
        ConversationEvent(
          subtype='resume',
          timestamp=_utc_now_iso(),
          session_id=path.stem,
          previous_session_id=self.resume_segment,
          verified=cuts.verified,
        )
      )
      timeline.extend(_fork_chunks(path.stem, cuts))
    else:
      timeline.append(Chunk(path.stem, 0, None))
    self._reset_prefix()
    self.state = ConversationState(lulid(), timeline)
    self.state.save(self.state_path)
    self._advance_prefix(self.state)
    log.info('conversation %s starts at segment %s', self.state.conversation_id, path.stem[:12])

  def _maybe_transition(self, new_path: Path) -> None:
    state = self.state
    assert state is not None
    old_stem = state.active_segment
    old_path = self._active_path()
    cuts = _fork_cuts(old_path, new_path)
    if cuts.verified:
      self._freeze_active(cuts.old_line_count)
      self._ensure_leave(timestamp=_mtime_iso(old_path))
      state.timeline.append(
        ConversationEvent(
          subtype='resume',
          timestamp=_utc_now_iso(),
          session_id=new_path.stem,
          previous_session_id=old_stem,
          verified=True,
        )
      )
      state.timeline.extend(_fork_chunks(new_path.stem, cuts))
      state.save(self.state_path)
      self._advance_prefix(state)
      self._uploaded_signature = None
      log.info(
        'segment %s continues conversation %s (forked from %s)',
        new_path.stem[:12],
        state.conversation_id,
        old_stem[:12],
      )
      return
    if not self._active_is_quiet(old_path):
      # a live active segment plus an unrelated newer jsonl: hold rather than
      # flip the conversation back and forth between two growing files
      log.warning(
        'segment %s does not continue %s and %s is still growing; holding',
        new_path.stem[:12],
        state.conversation_id,
        old_stem[:12],
      )
      return
    self._split(new_path, old_path)

  def _active_is_quiet(self, old_path: Path) -> bool:
    if self.started_after is None:
      return True
    try:
      stat = old_path.stat()
    except FileNotFoundError:
      return True
    if stat.st_mtime < self.started_after:
      return True
    signature = (old_path.stem, stat.st_mtime_ns, stat.st_size)
    return signature == self._active_signature

  def _split(self, new_path: Path, old_path: Path) -> None:
    """finalize the conversation and start a new one at `new_path` — the new
    segment does not carry the history, so continuing the timeline would join
    unrelated content."""
    old_state = self.state
    assert old_state is not None
    self._freeze_active(_line_count(old_path))
    self._ensure_leave(timestamp=_mtime_iso(old_path))
    old_state.save(self.state_path)
    # the upload raising leaves the old state saved with its leave event; the
    # next tick re-enters the transition and retries
    self._upload(old_state)
    self._reset_prefix()
    self.state = ConversationState(
      lulid(),
      [
        ConversationEvent(
          subtype='resume',
          timestamp=_utc_now_iso(),
          session_id=new_path.stem,
          previous_session_id=old_state.active_segment,
          previous_conversation_id=old_state.conversation_id,
          verified=False,
        ),
        Chunk(new_path.stem, 0, None),
      ],
    )
    self.state.save(self.state_path)
    self._advance_prefix(self.state)
    self._uploaded_signature = None
    log.info(
      'segment %s starts conversation %s (previous conversation %s finalized)',
      new_path.stem[:12],
      self.state.conversation_id,
      old_state.conversation_id,
    )

  def _freeze_active(self, line_count: int) -> None:
    """close the timeline's last chunk at `line_count` — or extend it, when a
    late flush grew the file past an earlier freeze."""
    chunk = self.state.last_chunk() if self.state is not None else None
    assert chunk is not None
    if chunk.end_line is None or chunk.end_line < line_count:
      chunk.end_line = line_count

  def _ensure_leave(self, timestamp: str) -> None:
    state = self.state
    assert state is not None
    last = state.timeline[-1]
    if isinstance(last, ConversationEvent) and last.subtype == 'leave':
      return
    state.timeline.append(
      ConversationEvent(subtype='leave', timestamp=timestamp, session_id=state.active_segment)
    )

  def _mark_same_segment_resume(self) -> None:
    """a trailing leave followed by new lines in the same segment file is a
    headless-style resume — claude appended to the file instead of forking."""
    state = self.state
    assert state is not None
    last = state.timeline[-1]
    if not isinstance(last, ConversationEvent) or last.subtype != 'leave':
      return
    if last.session_id != state.active_segment:
      return
    frozen_end = state.last_chunk().end_line
    if frozen_end is None or _line_count(self._active_path()) <= frozen_end:
      return
    state.timeline.append(
      ConversationEvent(
        subtype='resume',
        timestamp=_utc_now_iso(),
        session_id=state.active_segment,
        previous_session_id=state.active_segment,
        verified=True,
      )
    )
    state.timeline.append(Chunk(state.active_segment, frozen_end, None))
    state.save(self.state_path)
    self._advance_prefix(state)

  def _upload_if_changed(self) -> bool:
    state = self.state
    assert state is not None
    path = self._active_path()
    try:
      stat = path.stat()
    except FileNotFoundError:
      return False
    signature = (state.active_segment, stat.st_mtime_ns, stat.st_size)
    self._active_signature = signature
    if signature == self._uploaded_signature:
      return False
    self._mark_same_segment_resume()
    self._upload(state)
    self._uploaded_signature = signature
    return True

  def _reset_prefix(self) -> None:
    self.prefix_path.parent.mkdir(parents=True, exist_ok=True)
    self.prefix_path.write_bytes(b'')

  def _advance_prefix(self, state: ConversationState) -> None:
    """extend the cache to the timeline's current stable head — everything
    strictly before the last chunk, the only item later mutations can touch.
    called right after a transition, while the newly frozen chunk's source
    file certainly still exists. cache append precedes the state save: a crash
    between the two leaves extra cache bytes that the next integrity check
    truncates away."""
    target = _stable_item_count(state.timeline)
    if target <= state.prefix.items:
      return
    self._ensure_prefix_integrity(state)
    scan = _MetadataScan.from_snapshot(state.prefix.scan)
    lines = _compose_items(self.projects_dir, state.timeline[state.prefix.items : target], scan)
    data = _encode_lines(lines)
    with open(self.prefix_path, 'ab') as f:
      f.write(data)
    state.prefix = CachedPrefix(
      items=target,
      byte_count=state.prefix.byte_count + len(data),
      line_count=state.prefix.line_count + len(lines),
      scan=scan.to_snapshot(),
    )
    state.save(self.state_path)

  def _ensure_prefix_integrity(self, state: ConversationState) -> None:
    expected = state.prefix.byte_count
    try:
      size = self.prefix_path.stat().st_size
    except FileNotFoundError:
      size = None
    if size == expected:
      return
    if size is None and expected == 0:
      self._reset_prefix()
      return
    if size is not None and size > expected:
      # a torn append (crash between cache write and state save)
      os.truncate(self.prefix_path, expected)
      return
    self._rebuild_prefix(state)

  def _rebuild_prefix(self, state: ConversationState) -> None:
    """recompose the cache from the timeline's sources; a source cleaned up
    since its chunk froze degrades to a missing-segment marker."""
    log.warning('prefix cache %s out of sync; rebuilding', self.prefix_path)
    scan = _MetadataScan()
    lines = _compose_items(self.projects_dir, state.timeline[: state.prefix.items], scan)
    data = _encode_lines(lines)
    tmp = self.prefix_path.with_name(self.prefix_path.name + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(self.prefix_path)
    state.prefix = CachedPrefix(
      items=state.prefix.items,
      byte_count=len(data),
      line_count=len(lines),
      scan=scan.to_snapshot(),
    )
    state.save(self.state_path)

  def _upload(self, state: ConversationState) -> None:
    self._ensure_prefix_integrity(state)
    try:
      prefix_bytes = self.prefix_path.read_bytes()
    except FileNotFoundError:
      prefix_bytes = b''
    scan = _MetadataScan.from_snapshot(state.prefix.scan)
    tail_lines = _compose_items(self.projects_dir, state.timeline[state.prefix.items :], scan)
    composed = _Composed(
      prefix_bytes + _encode_lines(tail_lines),
      scan,
      state.prefix.line_count + len(tail_lines),
    )
    s3_key = f'logs/{self.workspace}/{state.conversation_id}.jsonl'
    self.store.put_log(s3_key, composed.body)
    self.store.put_item(_build_item(state, self.workspace, s3_key, composed))
    log.info(
      'synced conversation %s (%d bytes, %d lines, segment %s)',
      state.conversation_id[:13],
      len(composed.body),
      composed.line_count,
      state.active_segment[:12],
    )


def _stable_item_count(timeline: list[Chunk | ConversationEvent]) -> int:
  """how many leading timeline items are immutable: everything strictly before
  the last chunk (whose end_line a freeze may still set or extend)."""
  for index in range(len(timeline) - 1, -1, -1):
    if isinstance(timeline[index], Chunk):
      return index
  return 0


def _fork_chunks(segment: str, cuts: _ForkCuts) -> list[Chunk]:
  """the forked segment's own contribution: its pre-copy head (new-session
  ephemera written before the history copy) and its open tail after the copy."""
  chunks: list[Chunk] = []
  if cuts.verified and cuts.copy_start_line > 0:
    chunks.append(Chunk(segment, 0, cuts.copy_start_line))
  chunks.append(Chunk(segment, cuts.resume_start_line if cuts.verified else 0, None))
  return chunks


def _attempt(step: Callable[[], bool]) -> None:
  try:
    uploaded = step()
  except Exception as e:
    log.exception('sync failed')
    session_log_health.write('error', _exception_summary(e))
    return
  if uploaded:
    session_log_health.write('ok')


def _watch(engine: ConversationSync, interval: int) -> None:
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
    _attempt(engine.tick)
    stop.wait(interval)

  _attempt(engine.finalize)


def sync_session_log(
  watch: bool = False,
  interval: int = 15,
  workspace: Optional[str] = None,
  projects_dir: Optional[Path] = None,
  resume_segment: Optional[str] = None,
) -> int:
  workspace_name = workspace if workspace is not None else _workspace_name()
  if workspace_name is None:
    log.error('cannot determine workspace name; pass --workspace or set CW_COMMAND/CW_NAME')
    return 1

  try:
    config = _load_config()
  except credentials.SecretNotFound:
    log.error('config not found: session_log (run setup/bootstrap_session_log.sh)')
    session_log_health.write('error', 'config not found: session_log')
    return 1
  store = _Store(_create_session(config), config['bucket'], config['table'])

  src = projects_dir if projects_dir is not None else _projects_dir()
  engine = ConversationSync(
    src,
    workspace_name,
    store,
    started_after=time.time() if watch else None,
    resume_segment=resume_segment,
  )

  if watch:
    log.info('watching %s (interval=%ds, workspace=%s)', src, interval, workspace_name)
    _watch(engine, interval)
    return 0

  try:
    uploaded = engine.tick()
  except Exception as e:
    session_log_health.write('error', _exception_summary(e))
    raise
  if not uploaded:
    log.error('no session log found in %s', src)
    return 1
  session_log_health.write('ok')
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='sync Claude Code session logs to S3 + DynamoDB')
  parser.add_argument(
    '--watch', action='store_true', help='run the continuous sync daemon (one-shot otherwise)'
  )
  parser.add_argument(
    '--interval', type=int, default=15, help='daemon poll interval in seconds (default: 15)'
  )
  parser.add_argument(
    '--workspace', default=None, help='workspace name (default: from CW_COMMAND/CW_NAME)'
  )
  parser.add_argument(
    '--projects-dir',
    type=Path,
    default=None,
    help='claude projects dir to sync (default: derived from the config dir and cwd)',
  )
  parser.add_argument(
    '--resume-segment',
    default=None,
    help='claude session id this session resumes; seeds the conversation with '
    'that segment when no conversation state exists yet',
  )
  return sync_session_log(**parser.parse(argv))
