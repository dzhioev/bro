"""Local-filesystem trails store."""

import base64
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from bro.base.lulid import lulid
from bro.trails import backends, formats, importing, model, rows
from bro.trails.lineage import LineageDecision
from bro.trails.model import (
  UNREPORTED_END_INFERENCE,
  BlazeRequest,
  canonical_json_bytes,
  is_sha256,
  named_tool_digests,
  payload_sha256,
  validate_end,
)
from bro.trails.store import (
  AppendConflict,
  ToolNotFound,
  TrailNotFound,
  TrailsStore,
  delete_manifest,
  manifest_name,
  refuse_while_forked,
  refusing_invalid_requests,
)

_DEFAULT_PAGE_SIZE = 100
_UNREPORTED_AFTER = timedelta(hours=1)


class LocalStore(TrailsStore):
  def __init__(self, root: Path):
    self.root = root.expanduser().resolve()
    self.trails_directory = self.root / 'trails'
    self.manifests_directory = self.root / 'manifests'
    self.staging_directory = self.root / 'staging'
    self.trails_directory.mkdir(parents=True, exist_ok=True)

  def list_trails(
    self,
    *,
    harness: Optional[str] = None,
    bro: Optional[str] = None,
    forked_from: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
  ) -> dict:
    if sum(value is not None for value in (harness, bro, forked_from)) > 1:
      raise ValueError('only one of harness/bro/forked_from may be set')
    page_size = _DEFAULT_PAGE_SIZE if limit is None else limit
    if page_size < 1 or page_size > 100:
      raise ValueError('limit must be between 1 and 100')
    after = _decode_cursor(cursor) if cursor is not None else None
    headers: list[dict] = []
    for directory in self._trail_directories():
      header = self.get_trail(directory.name)
      if harness is not None and header.get('harness') != harness:
        continue
      if bro is not None and header.get('bro') != bro:
        continue
      if forked_from is not None and header.get('forked_from', {}).get('trail_id') != forked_from:
        continue
      started_at = header['started_at']
      if since is not None and started_at < since:
        continue
      if until is not None and started_at > until:
        continue
      key = (started_at, header['id'])
      if after is not None and key >= after:
        continue
      headers.append(header)
    headers.sort(key=lambda header: (header['started_at'], header['id']), reverse=True)
    page = headers[:page_size]
    next_cursor = None
    if len(headers) > page_size:
      last = page[-1]
      next_cursor = _encode_cursor((last['started_at'], last['id']))
    return {'trails': page, 'next': next_cursor}

  def get_trail(self, trail_id: str) -> dict:
    with self._locked(trail_id, shared=True):
      header = self._read_header(trail_id)
    return self._project_header(formats.upgrade_header(header))

  def find_segment_trails(self, segments: set[str]) -> list[dict]:
    """The headers of the trails recording one of `segments`."""
    if len(segments) == 0:
      return []
    return [
      header
      for directory in sorted(self._trail_directories())
      if (header := self.get_trail(directory.name)).get('native', {}).get('segment') in segments
    ]

  def holds_record(self, trail_ids: set[str], uuid: str) -> bool:
    """Whether any of the named trails stores the record `uuid`."""
    return any(
      row.get('uuid') == uuid for trail_id in sorted(trail_ids) for row in self._read_rows(trail_id)
    )

  def get_step(self, trail_id: str, step_id: int) -> dict:
    if step_id < 0:
      raise TrailNotFound(f'{trail_id}/{step_id}')
    with self._locked(trail_id, shared=True):
      formats.upgrade_header(self._read_header(trail_id))
      rows = self._read_rows(trail_id, step_id, step_id + 1)
    if len(rows) == 0:
      raise TrailNotFound(f'{trail_id}/{step_id}')
    return rows[0]

  def get_steps(
    self, trail_id: str, *, after: Optional[int] = None, limit: Optional[int] = None
  ) -> dict:
    page_size = _DEFAULT_PAGE_SIZE if limit is None else limit
    if page_size < 1 or page_size > 500:
      raise ValueError('limit must be between 1 and 500')
    with self._locked(trail_id, shared=True):
      formats.upgrade_header(self._read_header(trail_id))
      lines = self._read_row_lines(trail_id)
    start = 0 if after is None else max(0, after + 1)
    page = [
      formats.upgrade_row(row)
      for row in _parse_rows(trail_id, lines[start : start + page_size], start)
    ]
    next_cursor = page[-1]['step_id'] if len(lines) > start + page_size else None
    return {'steps': page, 'next': next_cursor}

  def get_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[int] = None,
    limit: Optional[int] = None,
  ) -> dict:
    page = self.get_steps(trail_id, after=after, limit=limit)
    header = self.get_trail(trail_id)
    messages = rows.project_messages(self._adapter(header['harness']), page['steps'], types)
    return {'messages': messages, 'next': page['next']}

  def get_launch_context(self, trail_id: str) -> Optional[Any]:
    with self._locked(trail_id, shared=True):
      formats.upgrade_header(self._read_header(trail_id))
      return self._read_launch_context(trail_id)

  def get_tool(self, sha256: str) -> Any:
    try:
      return json.loads(self._tool_path(sha256).read_bytes())
    except FileNotFoundError as exception:
      raise ToolNotFound(sha256) from exception

  def stored_trail_ids(self) -> list[str]:
    """The ids of every trail the root holds."""
    return sorted(directory.name for directory in self._trail_directories())

  def stored_header(self, trail_id: str) -> dict:
    """The header as stored, in the format it was written in."""
    with self._locked(trail_id, shared=True):
      return self._read_header(trail_id)

  def stored_rows(self, trail_id: str) -> list[dict]:
    """The rows as stored, each in the format it was written in."""
    with self._locked(trail_id, shared=True):
      return self._read_stored_rows(trail_id)

  def stored_launch_context(self, trail_id: str) -> Optional[Any]:
    with self._locked(trail_id, shared=True):
      return self._read_launch_context(trail_id)

  def blaze(self, request: BlazeRequest) -> dict:
    adapter = self._adapter(request.harness)
    with refusing_invalid_requests('blaze request'):
      adapter.validate_create(request.native)
      if request.harness == 'bro' and request.bro is None:
        raise ValueError('bro is required for the bro harness')
    decision = None
    forked_from = request.forked_from
    native = dict(request.native)
    if request.lineage is not None:
      decision = backends.resolve_lineage(adapter, request, self)
      if not decision.adopt:
        return {'adopted': False, 'reason': decision.reason}
      if decision.attach_to is not None:
        return self._attach(request, decision)
      forked_from = decision.forked_from
    if forked_from is not None:
      parent_id = forked_from['trail_id']
      native.update(rows.inherited_native(adapter, lambda: self.get_trail(parent_id)))
    if decision is not None:
      native.update(rows.minted_native(native, decision.chunks))
    with refusing_invalid_requests('blaze body'):
      records = adapter.open(request.body).records
    trail_id = lulid()
    started_at = _now_iso()
    directory = self._trail_directory(trail_id)
    with _creating_directory(directory):
      header: dict[str, Any] = {
        'id': trail_id,
        'format': model.TRAIL_FORMAT,
        'harness': request.harness,
        'version': request.version,
        'started_at': started_at,
        'end': None,
        'last_alive_at': started_at,
        'interactive': request.interactive,
        'surface': request.surface,
        'turn_count': 0,
        'native': native,
        'extent': 0,
      }
      if forked_from is not None:
        header['forked_from'] = forked_from
      for field in ('bro', 'hold', 'summoned_by', 'subject', 'location'):
        value = getattr(request, field)
        if value is not None:
          header[field] = value
      state = rows.AggregateState(header, adapter)
      prepared = rows.build_rows(
        trail_id=trail_id,
        offset=0,
        payloads=records,
        adapter=adapter,
        default_timestamp=started_at,
        state=state,
        seen_billing_keys=set(),
      )
      header.update(rows.state_fields(state, len(prepared)))
      self._write_rows(trail_id, prepared, append=False)
      launch_context = request.body.get('launch_context')
      if launch_context is not None:
        _atomic_json(directory / 'context.json', launch_context)
      _atomic_json(directory / 'header.json', header)
      (directory / '.lock').touch()
    return backends.blaze_result(trail_id, started_at, len(prepared), decision)

  def _attach(self, request: BlazeRequest, decision: LineageDecision) -> dict:
    """Reopen the trail the verdict attached to for this lifetime, at the extent
    it was verified against."""
    assert decision.attach_to is not None
    trail_id = decision.attach_to['trail_id']
    extent = decision.attach_to['extent']
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      self._migrate_locked(trail_id, header)
      if _extent(header) != extent:
        return {'adopted': False, 'reason': backends.ATTACH_CONTENDED}
      header.update(backends.attached_header(header, request))
      header['last_alive_at'] = _now_iso()
      _atomic_json(self._trail_directory(trail_id) / 'header.json', header)
    return backends.blaze_result(trail_id, header['started_at'], extent, decision)

  def append_records(
    self,
    trail_id: str,
    offset: int,
    records: list[Any],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    if offset < 0:
      raise ValueError('offset must be non-negative')
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      self._migrate_locked(trail_id, header)
      actual = _extent(header)
      expected_end = offset + len(records)
      if actual != offset:
        existing = self._read_rows(trail_id, offset, expected_end)
        hashes = [payload_sha256(record) for record in records]
        if actual == expected_end and [row.get('payload_sha256') for row in existing] == hashes:
          return {'extent': actual, 'appended': 0, 'duplicate': True}
        raise AppendConflict(offset, actual)
      self._store_tools({} if tools is None else tools)
      if len(records) == 0:
        return {'extent': actual, 'appended': 0}
      adapter = self._adapter(header['harness'])
      state = rows.AggregateState(header, adapter)
      prepared = rows.build_rows(
        trail_id=trail_id,
        offset=offset,
        payloads=records,
        adapter=adapter,
        default_timestamp=_now_iso(),
        state=state,
        seen_billing_keys=set(),
      )
      self._write_rows(trail_id, prepared, append=True)
      header.update(rows.state_fields(state, expected_end))
      header['last_alive_at'] = _now_iso()
      _atomic_json(self._trail_directory(trail_id) / 'header.json', header)
      return {'extent': expected_end, 'appended': len(prepared)}

  def set_subject(self, trail_id: str, subject: Optional[str]) -> dict:
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      header['subject'] = subject
      _atomic_json(self._trail_directory(trail_id) / 'header.json', header)
    return self._project_header(formats.upgrade_header(header))

  def end_trail(
    self,
    trail_id: str,
    reason: str,
    detail: Optional[str] = None,
  ) -> None:
    validate_end(reason, detail)
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      timestamp = _now_iso()
      end = {'at': timestamp, 'reason': reason}
      if detail is not None:
        end['detail'] = detail
      header['end'] = end
      header['last_alive_at'] = timestamp
      _atomic_json(self._trail_directory(trail_id) / 'header.json', header)

  def keepalive(self, trail_id: str) -> None:
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      header['last_alive_at'] = _now_iso()
      _atomic_json(self._trail_directory(trail_id) / 'header.json', header)

  def migrate_trail(self, trail_id: str) -> dict:
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      migrated_rows = self._migrate_locked(trail_id, header)
    return {
      'trail_id': trail_id,
      'format': model.TRAIL_FORMAT,
      'migrated_rows': migrated_rows,
    }

  def _migrate_locked(self, trail_id: str, header: dict) -> int:
    source_format = formats.stored_format(header, description=f'trail {trail_id} header')
    upgraded_header = formats.upgrade_header(header)
    if source_format == model.TRAIL_FORMAT:
      return 0
    stored_rows = self._read_stored_rows(trail_id)
    migrated_rows = sum(
      formats.stored_format(row, description=f'trail row {trail_id}/{row.get("step_id")}')
      != model.TRAIL_FORMAT
      for row in stored_rows
    )
    upgraded_rows = [formats.upgrade_row(row) for row in stored_rows]
    self._write_rows(trail_id, upgraded_rows, append=False)
    header.clear()
    header.update(upgraded_header)
    _atomic_json(self._trail_directory(trail_id) / 'header.json', header)
    return migrated_rows

  def delete_trail(self, trail_id: str) -> dict:
    directory = self._trail_directory(trail_id)
    if not directory.is_dir():
      raise TrailNotFound(trail_id)
    # the scan reads every trail's header under a shared lock of its own, this
    # one's included, so it cannot run inside the exclusive lock below
    refuse_while_forked(self, trail_id)
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      steps = self._read_rows(trail_id)
      at = _now_iso()
      manifest = self.manifests_directory / 'delete' / manifest_name(trail_id, at)
      _atomic_json(
        manifest,
        delete_manifest(trail_id=trail_id, at=at, header=header, steps=steps),
      )
      shutil.rmtree(directory)
    return {'trail_id': trail_id, 'extent': len(steps), 'manifest': str(manifest)}

  def begin_import(self, header: dict, *, launch_context: Optional[Any] = None) -> dict:
    with refusing_invalid_requests('imported header'):
      adapter = self._adapter(header['harness'])
      imported = importing.imported_header(header, adapter)
    trail_id = imported['id']
    directory = self._trail_directory(trail_id)
    importing.require_parents(imported, self._holds_trail)
    # the trail is written whole under staging and renamed into place, so a
    # reader never sees a half-written one and the rename decides which of two
    # begins won the id
    self.staging_directory.mkdir(exist_ok=True)
    staging = self.staging_directory / f'{trail_id}.{lulid()}'
    with _creating_directory(staging):
      _atomic_bytes(staging / 'steps.jsonl', b'')
      if launch_context is not None:
        _atomic_json(staging / 'context.json', launch_context)
      _atomic_json(staging / 'header.json', imported)
      (staging / '.lock').touch()
      try:
        os.rename(staging, directory)
      except OSError as exception:
        if exception.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
          raise
      else:
        return {'trail_id': trail_id, 'extent': 0, 'created': True}
    shutil.rmtree(staging)
    with self._locked(trail_id, shared=True):
      existing = self._read_header(trail_id)
      existing_context = self._read_launch_context(trail_id)
      stored = len(self._read_row_lines(trail_id))
    importing.verify_same_import(
      trail_id, adapter, existing, imported, existing_context, launch_context
    )
    return {'trail_id': trail_id, 'extent': stored, 'created': False}

  def import_rows(
    self,
    trail_id: str,
    offset: int,
    rows: list[dict],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    if offset < 0:
      raise ValueError('offset must be non-negative')
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      adapter = self._adapter(header['harness'])
      importing.validate_rows(trail_id, offset, rows, adapter)
      pending = importing.import_state(header)
      # the row file is what an import has written; the header's extent
      # follows it, a crash between the two leaving it behind
      stored = self._read_stored_rows(trail_id)
      actual = len(stored)
      if offset > actual:
        raise AppendConflict(offset, actual)
      present = min(actual - offset, len(rows))
      importing.verify_same_rows(trail_id, stored[offset : offset + present], rows[:present])
      remainder = rows[present:]
      importing.verify_room(trail_id, pending, actual, len(remainder))
      self._store_tools({} if tools is None else tools)
      self._require_tools(named_tool_digests(rows))
      if len(remainder) > 0:
        self._write_rows(trail_id, remainder, append=True)
      extent = actual + len(remainder)
      if _extent(header) != extent:
        header['extent'] = extent
        _atomic_json(self._trail_directory(trail_id) / 'header.json', header)
      if len(remainder) == 0:
        return {'extent': extent, 'appended': 0, 'duplicate': True}
      return {'extent': extent, 'appended': len(remainder)}

  def seal_import(self, trail_id: str) -> dict:
    with self._locked(trail_id, shared=False):
      header = self._read_header(trail_id)
      pending = importing.import_state(header)
      stored = self._read_rows(trail_id)
      if pending is None:
        return {'trail_id': trail_id, 'extent': len(stored), 'duplicate': True}
      importing.verify_complete(trail_id, pending, len(stored))
      adapter = self._adapter(header['harness'])
      fields = importing.sealed_fields(header, stored, adapter, pending['end'])
      for key in ('importing', 'last_billed_message_id'):
        header.pop(key, None)
      header.update(fields)
      _atomic_json(self._trail_directory(trail_id) / 'header.json', header)
    return {'trail_id': trail_id, 'extent': len(stored)}

  def close(self) -> None:
    pass

  @staticmethod
  def _adapter(harness: str) -> backends.Adapter:
    try:
      return backends.BACKENDS[harness]
    except KeyError as exception:
      raise ValueError(f'unsupported harness: {harness}') from exception

  def _trail_directory(self, trail_id: str) -> Path:
    if (
      len(trail_id) == 0
      or trail_id in {'.', '..', 'tools'}
      or '/' in trail_id
      or os.sep in trail_id
    ):
      raise ValueError(f'invalid trail id: {trail_id!r}')
    return self.trails_directory / trail_id

  def _holds_trail(self, trail_id: str) -> bool:
    return (self._trail_directory(trail_id) / 'header.json').is_file()

  def _trail_directories(self) -> list[Path]:
    return [
      directory
      for directory in self.trails_directory.iterdir()
      if directory.is_dir() and (directory / 'header.json').is_file()
    ]

  def _tool_path(self, sha256: str) -> Path:
    if not is_sha256(sha256):
      raise ValueError(f'invalid tool digest: {sha256!r}')
    return self.trails_directory / 'tools' / f'{sha256}.json'

  def _read_launch_context(self, trail_id: str) -> Optional[Any]:
    path = self._trail_directory(trail_id) / 'context.json'
    if not path.is_file():
      return None
    return json.loads(path.read_text())

  def _require_tools(self, digests: set[str]) -> None:
    for digest in sorted(digests):
      if not self._tool_path(digest).is_file():
        raise ValueError(f'tool blob {digest} is neither carried nor stored')

  @contextlib.contextmanager
  def _locked(self, trail_id: str, *, shared: bool) -> Iterator[None]:
    directory = self._trail_directory(trail_id)
    if not directory.is_dir():
      raise TrailNotFound(trail_id)
    lock_path = directory / '.lock'
    if shared:
      try:
        lock = lock_path.open('rb')
      except FileNotFoundError:
        lock = lock_path.open('a+b')
    else:
      lock = lock_path.open('a+b')
    with lock:
      fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
      yield

  def _read_header(self, trail_id: str) -> dict:
    path = self._trail_directory(trail_id) / 'header.json'
    try:
      value = json.loads(path.read_text())
    except FileNotFoundError as exception:
      raise TrailNotFound(trail_id) from exception
    if not isinstance(value, dict):
      raise ValueError(f'trail {trail_id} header must be an object')
    return value

  def _read_row_lines(self, trail_id: str) -> list[str]:
    path = self._trail_directory(trail_id) / 'steps.jsonl'
    try:
      return path.read_text().splitlines()
    except FileNotFoundError as exception:
      raise TrailNotFound(trail_id) from exception

  def _read_stored_rows(
    self, trail_id: str, start: int = 0, end: Optional[int] = None
  ) -> list[dict]:
    lines = self._read_row_lines(trail_id)
    return _parse_rows(trail_id, lines[start:end], start)

  def _read_rows(self, trail_id: str, start: int = 0, end: Optional[int] = None) -> list[dict]:
    return [formats.upgrade_row(row) for row in self._read_stored_rows(trail_id, start, end)]

  def _write_rows(self, trail_id: str, prepared: list[dict], *, append: bool) -> None:
    path = self._trail_directory(trail_id) / 'steps.jsonl'
    mode = 'ab' if append else 'wb'
    encoded = b''.join(
      json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode() + b'\n'
      for row in prepared
    )
    if not append:
      _atomic_bytes(path, encoded)
      return
    with path.open(mode) as stream:
      stream.write(encoded)
      stream.flush()
      os.fsync(stream.fileno())

  def _store_tools(self, tools: dict[str, Any]) -> None:
    if not isinstance(tools, dict):
      raise ValueError('tools must be an object keyed by sha256')
    for sha256, body in tools.items():
      payload = canonical_json_bytes(body)
      if not is_sha256(sha256) or _sha256(payload) != sha256:
        raise ValueError(f'tool blob hash mismatch: {sha256}')
      path = self._tool_path(sha256)
      if not path.exists():
        _atomic_bytes(path, payload)

  @staticmethod
  def _project_header(header: dict) -> dict:
    projected = dict(header)
    end = projected.get('end')
    if end is None:
      last_alive = datetime.fromisoformat(projected['last_alive_at'].replace('Z', '+00:00'))
      if datetime.now(UTC) - last_alive >= _UNREPORTED_AFTER:
        projected['end'] = {
          'at': projected['last_alive_at'],
          'inference': UNREPORTED_END_INFERENCE,
        }
    raw_usage = projected.get('native', {}).get('usage', {})
    if not isinstance(raw_usage, dict):
      raise ValueError('native.usage must be an object')
    projected['usage'] = raw_usage
    projected['models'] = sorted(raw_usage)
    return projected


def _parse_rows(trail_id: str, lines: list[str], start: int) -> list[dict]:
  """Rows for `lines`, the slice of the trail's stream starting at step `start` —
  a step id is its row's line ordinal in `steps.jsonl`."""
  rows = [json.loads(line) for line in lines]
  if not all(isinstance(row, dict) for row in rows):
    raise ValueError(f'trail {trail_id} steps must be objects')
  if len(rows) > 0 and rows[0]['step_id'] != start:
    raise ValueError(f'trail {trail_id} carries step {rows[0]["step_id"]} at line {start}')
  return rows


@contextlib.contextmanager
def _creating_directory(directory: Path) -> Iterator[None]:
  directory.mkdir(parents=False)
  try:
    yield
  except BaseException:
    shutil.rmtree(directory)
    raise


def _extent(header: dict) -> int:
  extent = header.get('extent')
  if not isinstance(extent, int) or isinstance(extent, bool) or extent < 0:
    raise ValueError('trail header has an invalid extent')
  return extent


def _atomic_json(path: Path, value: Any) -> None:
  _atomic_bytes(
    path,
    json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode(),
  )


def _atomic_bytes(path: Path, value: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
  temporary = Path(temporary_name)
  try:
    with os.fdopen(descriptor, 'wb') as stream:
      stream.write(value)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)
  except BaseException:
    temporary.unlink(missing_ok=True)
    raise


def _sha256(payload: bytes) -> str:
  return hashlib.sha256(payload).hexdigest()


def _encode_cursor(key: tuple[str, str]) -> str:
  raw = json.dumps(key, separators=(',', ':')).encode()
  return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
  try:
    value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
  except (ValueError, json.JSONDecodeError) as exception:
    raise ValueError('invalid trails cursor') from exception
  if (
    not isinstance(value, list)
    or len(value) != 2
    or not all(isinstance(item, str) for item in value)
  ):
    raise ValueError('invalid trails cursor')
  return value[0], value[1]


def _now_iso() -> str:
  return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
