import json
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

from session_log import health, sync
from session_log.sync import Chunk, ConversationEvent, ConversationState, ConversationSync

_STALE = 1_000.0
_LAUNCH = 2_000.0


def _redirect_health(monkeypatch, tmp_path) -> Path:
  path = tmp_path / 'health.json'
  monkeypatch.setattr(health, 'health_path', lambda: path)
  return path


def _record(
  uuid: str, session: str = 's', timestamp: str = '2026-07-13T10:00:00Z', **extra
) -> dict:
  return {'type': 'assistant', 'uuid': uuid, 'timestamp': timestamp, 'sessionId': session, **extra}


def _ephemeral(**extra) -> dict:
  return {'type': 'queue-operation', **extra}


def _write_segment(projects_dir: Path, stem: str, entries: list[dict], mtime: float) -> Path:
  path = projects_dir / f'{stem}.jsonl'
  path.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
  os.utime(path, (mtime, mtime))
  return path


class _FakeStore(sync._Store):
  def __init__(self) -> None:
    self.logs: dict[str, bytes] = {}
    self.items: list[dict] = []

  def put_log(self, key: str, body: bytes) -> None:
    self.logs[key] = body

  def put_item(self, item: dict) -> None:
    self.items.append(item)


def _make_engine(
  tmp_path: Path,
  started_after: Optional[float] = None,
  resume_segment: Optional[str] = None,
) -> tuple[ConversationSync, _FakeStore, Path]:
  projects_dir = tmp_path / 'config' / 'projects' / '-workspace'
  projects_dir.mkdir(parents=True, exist_ok=True)
  store = _FakeStore()
  engine = ConversationSync(
    projects_dir, 'ws', store, started_after=started_after, resume_segment=resume_segment
  )
  return engine, store, projects_dir


def _uploaded_lines(store: _FakeStore, conversation_id: str) -> list[dict]:
  body = store.logs[f'logs/ws/{conversation_id}.jsonl']
  return [json.loads(line) for line in body.decode().splitlines()]


def _shape(lines: list[dict]) -> list[tuple[Optional[str], Optional[str]]]:
  """(uuid, event subtype) per composed line — the artifact's structure."""
  return [(line.get('uuid'), line.get('subtype')) for line in lines]


class TestAdoption:
  def test_adopts_and_uploads_fresh_segment(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(projects_dir, 'seg-a', [_record('u1'), _record('u2')], _LAUNCH + 10)

    assert engine.tick() is True
    state = engine.state
    assert state is not None
    assert state.segments == ['seg-a']
    lines = _uploaded_lines(store, state.conversation_id)
    assert [line['uuid'] for line in lines] == ['u1', 'u2']
    item = store.items[-1]
    assert item['session_id'] == state.conversation_id
    assert json.loads(item['segments']) == ['seg-a']

  def test_ignores_segments_older_than_the_launch(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(projects_dir, 'stale', [_record('u1')], _STALE)

    assert engine.tick() is False
    assert engine.state is None
    assert store.logs == {}

  def test_one_shot_adopts_the_newest_regardless_of_age(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=None)
    _write_segment(projects_dir, 'stale', [_record('u1')], _STALE)

    assert engine.tick() is True
    state = engine.state
    assert state is not None
    assert state.segments == ['stale']
    assert len(store.logs) == 1

  def test_unchanged_segment_is_not_reuploaded(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    path = _write_segment(projects_dir, 'seg-a', [_record('u1')], _LAUNCH + 10)

    assert engine.tick() is True
    assert engine.tick() is False
    _write_segment(projects_dir, 'seg-a', [_record('u1'), _record('u2')], _LAUNCH + 20)
    assert engine.tick() is True
    assert path.stem in json.loads(store.items[-1]['segments'])

  def test_state_survives_engine_restarts(self, tmp_path):
    engine, _, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(projects_dir, 'seg-a', [_record('u1')], _LAUNCH + 10)
    engine.tick()
    state = engine.state
    assert state is not None

    engine2, _, _ = _make_engine(tmp_path, started_after=_LAUNCH + 100)
    assert engine2.state is not None
    assert engine2.state.conversation_id == state.conversation_id
    assert engine2.state.segments == ['seg-a']


class TestForkTransition:
  def _adopted(self, tmp_path) -> tuple[ConversationSync, _FakeStore, Path, str]:
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(
      projects_dir,
      'seg-a',
      [
        _ephemeral(),
        _record('u1', session='seg-a'),
        _record('u2', session='seg-a'),
        _record('u3', session='seg-a'),
      ],
      _LAUNCH + 10,
    )
    engine.tick()
    assert engine.state is not None
    return engine, store, projects_dir, engine.state.conversation_id

  def test_verified_fork_continues_the_conversation(self, tmp_path):
    engine, store, projects_dir, conversation_id = self._adopted(tmp_path)
    # the fork re-serializes history (uuids kept, sessionId rewritten, ephemera
    # dropped) after its own head records, then appends the new turns
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _ephemeral(),
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
        _record('u4', session='seg-b'),
      ],
      _LAUNCH + 20,
    )

    assert engine.tick() is True
    state = engine.state
    assert state is not None
    assert state.conversation_id == conversation_id
    assert state.segments == ['seg-a', 'seg-b']

    lines = _uploaded_lines(store, conversation_id)
    assert _shape(lines) == [
      (None, None),  # seg-a's own ephemeral record, preserved
      ('u1', None),
      ('u2', None),
      ('u3', None),
      (None, 'leave'),
      (None, 'resume'),
      (None, None),  # seg-b's pre-copy head ephemeral record
      ('u4', None),
    ]
    # history records come from seg-a's original file, not the fork's rewrite
    assert [line['sessionId'] for line in lines if line.get('uuid') in ('u1', 'u2', 'u3')] == [
      'seg-a',
      'seg-a',
      'seg-a',
    ]
    resume = lines[5]
    assert resume['previousSessionId'] == 'seg-a'
    assert resume['historyVerified'] is True
    assert json.loads(store.items[-1]['segments']) == ['seg-a', 'seg-b']

  def test_unverified_fork_starts_a_new_conversation(self, tmp_path):
    engine, store, projects_dir, old_conversation = self._adopted(tmp_path)
    _write_segment(projects_dir, 'seg-b', [_record('x1'), _record('x2')], _LAUNCH + 20)
    # the active segment is quiet (unchanged since the last tick), so the split
    # is allowed on the next tick
    assert engine.tick() is True

    state = engine.state
    assert state is not None
    assert state.conversation_id != old_conversation
    assert state.segments == ['seg-b']

    old_lines = _uploaded_lines(store, old_conversation)
    assert old_lines[-1]['type'] == sync.EVENT_TYPE
    assert old_lines[-1]['subtype'] == 'leave'

    new_lines = _uploaded_lines(store, state.conversation_id)
    assert new_lines[0]['subtype'] == 'resume'
    assert new_lines[0]['historyVerified'] is False
    assert new_lines[0]['previousSessionId'] == 'seg-a'
    assert new_lines[0]['previousConversationId'] == old_conversation
    assert [line.get('uuid') for line in new_lines[1:]] == ['x1', 'x2']

  def test_growing_active_segment_holds_an_unverified_split(self, tmp_path):
    engine, store, projects_dir, conversation_id = self._adopted(tmp_path)
    # the active segment grew since the last observation AND an unrelated newer
    # file appeared: hold — keep syncing the active segment
    _write_segment(
      projects_dir,
      'seg-a',
      [_record('u1'), _record('u2'), _record('u3'), _record('u4')],
      _LAUNCH + 20,
    )
    _write_segment(projects_dir, 'foreign', [_record('x1')], _LAUNCH + 30)

    assert engine.tick() is True
    state = engine.state
    assert state is not None
    assert state.conversation_id == conversation_id
    assert state.segments == ['seg-a']
    assert [line.get('uuid') for line in _uploaded_lines(store, conversation_id)][-1] == 'u4'

    # once the active segment goes quiet the split happens
    assert engine.tick() is True
    assert engine.state is not None
    assert engine.state.segments == ['foreign']

  def test_consumed_segments_are_never_readopted(self, tmp_path):
    engine, _, projects_dir, conversation_id = self._adopted(tmp_path)
    _write_segment(
      projects_dir,
      'seg-b',
      [_record('u1'), _record('u2'), _record('u3'), _record('u4')],
      _LAUNCH + 20,
    )
    engine.tick()
    # a stray late write makes the consumed segment newest again; it must not
    # flip the conversation back
    _write_segment(
      projects_dir, 'seg-a', [_record('u1'), _record('u2'), _record('u3')], _LAUNCH + 30
    )
    engine.tick()
    state = engine.state
    assert state is not None
    assert state.conversation_id == conversation_id
    assert state.segments == ['seg-a', 'seg-b']


class TestLeaveResume:
  def test_finalize_appends_a_trailing_leave(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(projects_dir, 'seg-a', [_record('u1'), _record('u2')], _LAUNCH + 10)
    engine.tick()

    assert engine.finalize() is True
    state = engine.state
    assert state is not None
    lines = _uploaded_lines(store, state.conversation_id)
    assert lines[-1]['subtype'] == 'leave'
    assert lines[-1]['sessionId'] == 'seg-a'
    assert lines[-2]['uuid'] == 'u2'

  def test_finalize_twice_keeps_one_leave(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(projects_dir, 'seg-a', [_record('u1')], _LAUNCH + 10)
    engine.tick()
    engine.finalize()
    engine.finalize()

    state = engine.state
    assert state is not None
    events = [line for line in _uploaded_lines(store, state.conversation_id) if 'subtype' in line]
    assert [e['subtype'] for e in events] == ['leave']

  def test_same_segment_growth_after_a_leave_marks_a_resume(self, tmp_path):
    engine, _, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(projects_dir, 'seg-a', [_record('u1'), _record('u2')], _LAUNCH + 10)
    engine.tick()
    engine.finalize()

    # a headless resume appends to the same segment file; a fresh engine (the
    # next session's daemon) picks the state up from disk
    engine2, store2, _ = _make_engine(tmp_path, started_after=_LAUNCH + 100)
    _write_segment(
      projects_dir, 'seg-a', [_record('u1'), _record('u2'), _record('u3')], _LAUNCH + 110
    )
    assert engine2.tick() is True

    state = engine2.state
    assert state is not None
    lines = _uploaded_lines(store2, state.conversation_id)
    assert _shape(lines) == [
      ('u1', None),
      ('u2', None),
      (None, 'leave'),
      (None, 'resume'),
      ('u3', None),
    ]

  def test_resume_segment_seeds_the_conversation_without_state(self, tmp_path):
    # a resume of a session synced before conversations were tracked: no state,
    # but the runner names the segment the launch resumed — its original
    # records open the conversation
    engine, store, projects_dir = _make_engine(
      tmp_path, started_after=_LAUNCH, resume_segment='seg-a'
    )
    _write_segment(
      projects_dir,
      'seg-a',
      [_record('u1', session='seg-a'), _record('u2', session='seg-a')],
      _STALE,
    )
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
      ],
      _LAUNCH + 10,
    )

    assert engine.tick() is True
    state = engine.state
    assert state is not None
    assert state.segments == ['seg-a', 'seg-b']
    lines = _uploaded_lines(store, state.conversation_id)
    assert _shape(lines) == [
      ('u1', None),
      ('u2', None),
      (None, 'leave'),
      (None, 'resume'),
      ('u3', None),
    ]
    assert [line['sessionId'] for line in lines if line.get('uuid') in ('u1', 'u2')] == [
      'seg-a',
      'seg-a',
    ]
    resume = lines[3]
    assert resume['previousSessionId'] == 'seg-a'
    assert resume['historyVerified'] is True

  def test_resume_segment_without_verification_starts_bare(self, tmp_path):
    engine, store, projects_dir = _make_engine(
      tmp_path, started_after=_LAUNCH, resume_segment='gone'
    )
    _write_segment(projects_dir, 'seg-b', [_record('u1')], _LAUNCH + 10)

    assert engine.tick() is True
    state = engine.state
    assert state is not None
    assert state.segments == ['seg-b']
    lines = _uploaded_lines(store, state.conversation_id)
    assert lines[0]['subtype'] == 'resume'
    assert lines[0]['historyVerified'] is False
    assert lines[1]['uuid'] == 'u1'


class TestPrefixCache:
  def _forked(self, tmp_path) -> tuple[ConversationSync, _FakeStore, Path, str]:
    """a conversation that crossed one verified fork: seg-a frozen, seg-b active."""
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(
      projects_dir,
      'seg-a',
      [_ephemeral(), _record('u1', session='seg-a'), _record('u2', session='seg-a')],
      _LAUNCH + 10,
    )
    engine.tick()
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
      ],
      _LAUNCH + 20,
    )
    engine.tick()
    state = engine.state
    assert state is not None
    assert state.segments == ['seg-a', 'seg-b']
    return engine, store, projects_dir, state.conversation_id

  def test_frozen_content_survives_segment_file_removal(self, tmp_path):
    engine, store, projects_dir, conversation_id = self._forked(tmp_path)
    (projects_dir / 'seg-a.jsonl').unlink()
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
        _record('u4', session='seg-b'),
      ],
      _LAUNCH + 30,
    )

    assert engine.tick() is True
    lines = _uploaded_lines(store, conversation_id)
    assert [line['sessionId'] for line in lines if line.get('uuid') in ('u1', 'u2')] == [
      'seg-a',
      'seg-a',
    ]
    assert all(line.get('subtype') != 'missing-segment' for line in lines)
    assert lines[-1]['uuid'] == 'u4'

  def test_torn_cache_append_is_truncated_away(self, tmp_path):
    engine, _, projects_dir, conversation_id = self._forked(tmp_path)
    with open(engine.prefix_path, 'ab') as f:
      f.write(b'{"torn": true}\n')

    engine2, store2, _ = _make_engine(tmp_path, started_after=_LAUNCH + 100)
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
        _record('u4', session='seg-b'),
      ],
      _LAUNCH + 110,
    )
    assert engine2.tick() is True
    lines = _uploaded_lines(store2, conversation_id)
    assert all('torn' not in line for line in lines)
    assert [line.get('uuid') for line in lines if line.get('uuid') is not None] == [
      'u1',
      'u2',
      'u3',
      'u4',
    ]

  def test_missing_cache_rebuilds_from_sources(self, tmp_path):
    engine, _, projects_dir, conversation_id = self._forked(tmp_path)
    engine.prefix_path.unlink()

    engine2, store2, _ = _make_engine(tmp_path, started_after=_LAUNCH + 100)
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
        _record('u4', session='seg-b'),
      ],
      _LAUNCH + 110,
    )
    assert engine2.tick() is True
    lines = _uploaded_lines(store2, conversation_id)
    assert [line['sessionId'] for line in lines if line.get('uuid') in ('u1', 'u2')] == [
      'seg-a',
      'seg-a',
    ]

  def test_rebuild_without_sources_degrades_to_a_marker(self, tmp_path):
    engine, _, projects_dir, conversation_id = self._forked(tmp_path)
    engine.prefix_path.unlink()
    (projects_dir / 'seg-a.jsonl').unlink()

    engine2, store2, _ = _make_engine(tmp_path, started_after=_LAUNCH + 100)
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
        _record('u4', session='seg-b'),
      ],
      _LAUNCH + 110,
    )
    assert engine2.tick() is True
    lines = _uploaded_lines(store2, conversation_id)
    markers = [line for line in lines if line.get('subtype') == 'missing-segment']
    assert [marker['sessionId'] for marker in markers] == ['seg-a']
    assert lines[-1]['uuid'] == 'u4'

  def test_metadata_survives_via_the_scan_snapshot(self, tmp_path):
    engine, store, projects_dir = _make_engine(tmp_path, started_after=_LAUNCH)
    _write_segment(
      projects_dir,
      'seg-a',
      [
        {
          'type': 'user',
          'uuid': 'u1',
          'timestamp': '2026-07-01T10:00:00Z',
          'version': '2.1.195',
          'sessionId': 'seg-a',
          'message': {'content': 'hello'},
        },
        _record('u2', session='seg-a'),
      ],
      _LAUNCH + 10,
    )
    engine.tick()
    _write_segment(
      projects_dir,
      'seg-b',
      [
        _record('u1', session='seg-b'),
        _record('u2', session='seg-b'),
        _record('u3', session='seg-b'),
      ],
      _LAUNCH + 20,
    )
    engine.tick()
    # the subject and start time live in the frozen prefix; the upload's scan
    # resumes from the snapshot instead of re-parsing the cache
    item = store.items[-1]
    assert item['subject'] == 'hello'
    assert item['started_at'] == '2026-07-01T10:00:00Z'


class TestCompose:
  def test_missing_frozen_segment_degrades_to_a_marker(self, tmp_path):
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    _write_segment(projects_dir, 'seg-b', [_record('u4')], _STALE)
    timeline: list = [
      Chunk('seg-a', 0, 2),
      ConversationEvent('leave', '2026-07-13T10:01:00Z', 'seg-a'),
      ConversationEvent('resume', '2026-07-13T11:00:00Z', 'seg-b', previous_session_id='seg-a'),
      Chunk('seg-b', 0, None),
    ]
    lines = [
      json.loads(line) for line in sync._compose_items(projects_dir, timeline, sync._MetadataScan())
    ]
    assert lines[0]['subtype'] == 'missing-segment'
    assert lines[0]['sessionId'] == 'seg-a'
    assert [line.get('subtype') for line in lines[1:]] == ['leave', 'resume', None]
    assert lines[-1]['uuid'] == 'u4'

  def test_metadata_scanned_from_content_not_events(self, tmp_path):
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    _write_segment(
      projects_dir,
      'seg',
      [
        {
          'type': 'user',
          'timestamp': '2026-07-01T10:00:00Z',
          'version': '2.1.195',
          'message': {'content': 'hello'},
          'uuid': 'u1',
        },
        {
          'type': 'assistant',
          'version': '2.1.195',
          'message': {'model': 'claude-opus-4-8', 'content': [{'type': 'text', 'text': 'hi'}]},
          'uuid': 'u2',
        },
      ],
      _STALE,
    )
    timeline: list = [
      ConversationEvent('resume', '2026-06-30T09:00:00Z', 'seg', verified=False),
      Chunk('seg', 0, None),
    ]
    scan = sync._MetadataScan()
    lines = sync._compose_items(projects_dir, timeline, scan)
    assert scan.subject == 'hello'
    assert scan.model == 'claude-opus-4-8'
    assert scan.version == '2.1.195'
    # the head event's timestamp must not become the conversation start
    assert scan.started_at == '2026-07-01T10:00:00Z'
    assert len(lines) == 3


class TestRaisedScan:
  """the `raised` extraction: a raise tool call marks the conversation aborted
  with its reason; a later real user message (a resume moving past the abort)
  clears the mark."""

  def _raise_record(self, reason: str) -> dict:
    return {
      'type': 'assistant',
      'message': {
        'content': [
          {'type': 'tool_use', 'name': 'mcp__bro__raise', 'input': {'reason': reason}},
        ],
      },
    }

  def test_raise_call_sets_the_reason(self):
    scan = sync._MetadataScan()
    scan.feed({'type': 'user', 'message': {'content': 'do the thing'}})
    scan.feed(self._raise_record('missing api key'))
    assert scan.raised == 'missing api key'

  def test_other_tool_calls_do_not_mark(self):
    scan = sync._MetadataScan()
    scan.feed(
      {
        'type': 'assistant',
        'message': {'content': [{'type': 'tool_use', 'name': 'mcp__flow__add_task', 'input': {}}]},
      }
    )
    assert scan.raised is None

  def test_a_real_user_message_clears_the_mark(self):
    scan = sync._MetadataScan()
    scan.feed(self._raise_record('missing api key'))
    scan.feed({'type': 'user', 'message': {'content': 'resumed: key added, carry on'}})
    assert scan.raised is None

  def test_a_tool_result_record_does_not_clear_the_mark(self):
    # the raise call's own result comes back as a user-type record with only a
    # tool_result block — it must not read as the conversation moving on
    scan = sync._MetadataScan()
    scan.feed(self._raise_record('missing api key'))
    scan.feed(
      {
        'type': 'user',
        'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]},
      }
    )
    assert scan.raised == 'missing api key'

  def test_raised_survives_the_snapshot_round_trip(self):
    scan = sync._MetadataScan()
    scan.feed(self._raise_record('missing api key'))
    restored = sync._MetadataScan.from_snapshot(scan.to_snapshot())
    assert restored.raised == 'missing api key'


class TestState:
  def test_round_trips_the_timeline(self, tmp_path):
    path = tmp_path / 'state.json'
    state = ConversationState(
      'conv',
      [
        Chunk('a', 0, 5),
        ConversationEvent('leave', 't1', 'a'),
        ConversationEvent('resume', 't2', 'b', previous_session_id='a', verified=True),
        Chunk('b', 0, 2),
        Chunk('b', 7, None),
      ],
    )
    state.save(path)
    loaded = ConversationState.load(path)
    assert loaded == state
    assert loaded is not None
    assert loaded.segments == ['a', 'b']
    assert loaded.active_segment == 'b'

  def test_corrupt_state_starts_fresh(self, tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{not json')
    assert ConversationState.load(path) is None

  def test_unknown_timeline_kind_starts_fresh(self, tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'conversation_id': 'c', 'timeline': [{'kind': 'mystery'}]}))
    assert ConversationState.load(path) is None


class TestBuildItem:
  def _composed_item(self, tmp_path, monkeypatch, env: Optional[dict] = None) -> dict:
    for key, value in (env if env is not None else {}).items():
      monkeypatch.setenv(key, value)
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    _write_segment(
      projects_dir,
      'seg',
      [
        {
          'type': 'user',
          'timestamp': '2026-07-01T10:00:00Z',
          'version': '2.1.195',
          'message': {'content': 'hello'},
        }
      ],
      _STALE,
    )
    state = ConversationState('conv', [Chunk('seg', 0, None)])
    scan = sync._MetadataScan()
    lines = sync._compose_items(projects_dir, state.timeline, scan)
    composed = sync._Composed(sync._encode_lines(lines), scan, len(lines))
    return sync._build_item(state, 'ws', 'logs/ws/conv.jsonl', composed)

  def test_version_and_context_into_item(self, monkeypatch, tmp_path):
    records = [{'kind': 'git', 'subtype': 'state', 'title': 'git', 'fields': {'branch': 'b'}}]
    item = self._composed_item(
      tmp_path, monkeypatch, env={'CW_SESSION_CONTEXT': json.dumps(records)}
    )
    assert item['claude_code_version'] == '2.1.195'
    assert json.loads(item['context']) == records
    assert item['session_id'] == 'conv'
    assert json.loads(item['segments']) == ['seg']

  def test_context_absent_when_env_unset(self, monkeypatch, tmp_path):
    monkeypatch.delenv('CW_SESSION_CONTEXT', raising=False)
    item = self._composed_item(tmp_path, monkeypatch)
    assert 'context' not in item

  def test_raised_into_item(self, tmp_path):
    projects_dir = tmp_path / 'projects'
    projects_dir.mkdir()
    _write_segment(
      projects_dir,
      'seg',
      [
        {'type': 'user', 'timestamp': '2026-07-01T10:00:00Z', 'message': {'content': 'do it'}},
        {
          'type': 'assistant',
          'message': {
            'content': [
              {'type': 'tool_use', 'name': 'mcp__bro__raise', 'input': {'reason': 'no api key'}},
            ],
          },
        },
      ],
      _STALE,
    )
    state = ConversationState('conv', [Chunk('seg', 0, None)])
    scan = sync._MetadataScan()
    lines = sync._compose_items(projects_dir, state.timeline, scan)
    composed = sync._Composed(sync._encode_lines(lines), scan, len(lines))
    item = sync._build_item(state, 'ws', 'logs/ws/conv.jsonl', composed)
    assert item['raised'] == 'no api key'


class TestHealthOnOneShot:
  def _stub_store(self, monkeypatch, store) -> None:
    monkeypatch.setattr(sync, '_load_config', lambda: {'bucket': 'b', 'table': 't'})
    monkeypatch.setattr(sync, '_create_session', lambda config: MagicMock())
    monkeypatch.setattr(sync, '_Store', lambda session, bucket, table: store)

  def test_success_writes_ok(self, monkeypatch, tmp_path):
    _redirect_health(monkeypatch, tmp_path)
    self._stub_store(monkeypatch, _FakeStore())
    projects_dir = tmp_path / 'config' / 'projects' / '-ws'
    projects_dir.mkdir(parents=True)
    _write_segment(projects_dir, 'seg-a', [_record('u1')], _STALE)
    assert sync.sync_session_log(workspace='ws', projects_dir=projects_dir) == 0
    assert health.is_failing() is False

  def test_failure_writes_error_and_reraises(self, monkeypatch, tmp_path):
    _redirect_health(monkeypatch, tmp_path)
    broken = _FakeStore()

    def boom(key, body):
      raise RuntimeError('AccessDeniedException: PutItem')

    broken.put_log = boom
    self._stub_store(monkeypatch, broken)
    projects_dir = tmp_path / 'config' / 'projects' / '-ws'
    projects_dir.mkdir(parents=True)
    _write_segment(projects_dir, 'seg-a', [_record('u1')], _STALE)
    try:
      sync.sync_session_log(workspace='ws', projects_dir=projects_dir)
      raised = False
    except RuntimeError:
      raised = True
    assert raised
    assert health.is_failing() is True

  def test_missing_config_writes_error(self, monkeypatch, tmp_path):
    from base import credentials

    _redirect_health(monkeypatch, tmp_path)

    def _missing():
      raise credentials.SecretNotFound('session_log')

    monkeypatch.setattr(sync, '_load_config', _missing)
    assert sync.sync_session_log(workspace='ws') == 1
    assert health.is_failing() is True

  def test_empty_projects_dir_errors(self, monkeypatch, tmp_path):
    _redirect_health(monkeypatch, tmp_path)
    self._stub_store(monkeypatch, _FakeStore())
    projects_dir = tmp_path / 'config' / 'projects' / '-ws'
    projects_dir.mkdir(parents=True)
    assert sync.sync_session_log(workspace='ws', projects_dir=projects_dir) == 1
