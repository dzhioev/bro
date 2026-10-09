import contextlib
import json
import os
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from bro.llm import usage
from bro.llm.usage import Usage
from ride.claude.usage_publisher import SessionUsage, publishing_usage

OPUS = 'claude-opus-5'
HAIKU = 'claude-haiku-4-5-20251001'
SEGMENT = '786d6a80-6929-4c9b-aac7-4fdfbe98ec3c'


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


def _assistant(message_id: str, model: str = OPUS, *, version: str = '2.1.300', **counts) -> dict:
  return {
    'type': 'assistant',
    'version': version,
    'message': {
      'id': message_id,
      'model': model,
      'usage': {
        'input_tokens': counts.get('input', 0),
        'cache_creation_input_tokens': counts.get('cache_write', 0),
        'cache_read_input_tokens': counts.get('cache_read', 0),
        'output_tokens': counts.get('output', 0),
      },
      'content': [{'type': 'text', 'text': 'ok'}],
    },
  }


def _append(path: Path, *records: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('a') as stream:
    for record in records:
      stream.write(json.dumps(record) + '\n')


def _session(tmp_path: Path) -> tuple[Path, SessionUsage]:
  projects = tmp_path / 'projects'
  projects.mkdir()
  return projects, SessionUsage(projects, started_after=time.time() - 1)


class TestSessionUsage:
  def test_sums_the_segment_and_its_subagents_per_model(self, tmp_path):
    projects, session = _session(tmp_path)
    _append(projects / f'{SEGMENT}.jsonl', _assistant('m1', input=2, cache_read=5, output=7))
    subagents = projects / SEGMENT / 'subagents'
    _append(subagents / 'agent-a.jsonl', _assistant('m2', output=11))
    _append(subagents / 'agent-b.jsonl', _assistant('m3', HAIKU, output=5))
    assert session.refresh() == Usage(
      agent='Claude Code 2.1.300',
      per_model={OPUS: C(input=2, cache_read=5, output=18), HAIKU: C(output=5)},
    )

  def test_a_response_written_as_several_records_bills_once(self, tmp_path):
    projects, session = _session(tmp_path)
    split = _assistant('m1', output=7)
    _append(projects / f'{SEGMENT}.jsonl', split, split, split)
    current = session.refresh()
    assert current is not None
    assert current.per_model == {OPUS: C(output=7)}

  def test_a_turn_claude_generated_itself_bills_nothing(self, tmp_path):
    projects, session = _session(tmp_path)
    _append(
      projects / f'{SEGMENT}.jsonl',
      _assistant('m1', output=7),
      _assistant('m2', '<synthetic>', output=999),
    )
    current = session.refresh()
    assert current is not None
    assert current.per_model == {OPUS: C(output=7)}

  def test_reads_each_line_once_as_the_transcript_grows(self, tmp_path):
    projects, session = _session(tmp_path)
    segment = projects / f'{SEGMENT}.jsonl'
    _append(segment, _assistant('m1', output=7))
    session.refresh()
    with segment.open('a') as stream:
      stream.write(json.dumps(_assistant('m2', output=3))[:20])
    current = session.refresh()
    assert current is not None
    assert current.per_model == {OPUS: C(output=7)}
    with segment.open('a') as stream:
      stream.write(json.dumps(_assistant('m2', output=3))[20:] + '\n')
    current = session.refresh()
    assert current is not None
    assert current.per_model == {OPUS: C(output=10)}

  def test_the_agent_names_the_version_the_transcript_records(self, tmp_path):
    projects, session = _session(tmp_path)
    _append(
      projects / f'{SEGMENT}.jsonl',
      _assistant('m1', output=1, version='2.1.300'),
      _assistant('m2', output=1, version='2.1.301'),
    )
    current = session.refresh()
    assert current is not None
    assert current.agent == 'Claude Code 2.1.301'

  def test_nothing_billed_yet_is_no_usage(self, tmp_path):
    projects, session = _session(tmp_path)
    assert session.refresh() is None
    _append(projects / f'{SEGMENT}.jsonl', {'type': 'user', 'version': '2.1.300'})
    assert session.refresh() is None

  def test_a_segment_from_before_the_session_is_not_its_own(self, tmp_path):
    projects = tmp_path / 'projects'
    segment = projects / f'{SEGMENT}.jsonl'
    _append(segment, _assistant('m1', output=7))
    os.utime(segment, (0, 0))
    assert SessionUsage(projects, started_after=time.time()).refresh() is None

  def test_a_newer_segment_starts_the_count_afresh(self, tmp_path):
    projects, session = _session(tmp_path)
    older = projects / f'{SEGMENT}.jsonl'
    _append(older, _assistant('m1', output=7))
    session.refresh()
    newer = projects / 'b1f6c2e0-0000-4000-8000-000000000000.jsonl'
    _append(newer, _assistant('m2', output=3))
    os.utime(older, (time.time() - 0.5, time.time() - 0.5))
    current = session.refresh()
    assert current is not None
    assert current.per_model == {OPUS: C(output=3)}


def _await_published(target: Path) -> None:
  """wait until `target` holds billed usage, or no longer holds any to credit."""
  deadline = time.monotonic() + 10
  while True:
    try:
      if usage.read_usage_file(target) is not None:
        return
    except usage.UsageUnavailable:
      return
    assert time.monotonic() < deadline, 'the usage file never held billed usage'
    time.sleep(0.05)


@contextlib.contextmanager
def _restoring_modes(*paths: Path) -> Generator[None]:
  modes = [(path, path.stat().st_mode & 0o777) for path in paths]
  try:
    yield
  finally:
    for path, mode in modes:
      path.chmod(mode)


_ROOT = os.geteuid() == 0


class TestPublishingUsage:
  def test_keeps_the_file_holding_the_sessions_usage(self, tmp_path):
    projects = tmp_path / 'projects'
    projects.mkdir()
    target = tmp_path / 'state' / 'usage.json'
    with publishing_usage(projects, target):
      _append(projects / f'{SEGMENT}.jsonl', _assistant('m1', output=7))
      _await_published(target)
      assert usage.read_usage_file(target) == Usage(
        agent='Claude Code 2.1.300', per_model={OPUS: C(output=7)}
      )
    with pytest.raises(usage.UsageUnavailable, match='no longer keeps it current'):
      usage.read_usage_file(target)

  def test_reads_what_the_transcripts_hold_as_it_ends(self, tmp_path):
    projects = tmp_path / 'projects'
    projects.mkdir()
    target = tmp_path / 'usage.json'
    segment = projects / f'{SEGMENT}.jsonl'
    with publishing_usage(projects, target):
      _append(segment, _assistant('m1', output=7))
      _await_published(target)
      _append(segment, _assistant('m2', output=3))
    assert json.loads(target.read_text())['models'] == {OPUS: C(output=10)}

  def test_a_failure_fails_the_sessions_reads(self, tmp_path, monkeypatch):
    projects = tmp_path / 'projects'
    projects.mkdir()
    target = tmp_path / 'usage.json'
    unversioned = _assistant('m1', output=7)
    del unversioned['version']
    with publishing_usage(projects, target):
      _append(projects / f'{SEGMENT}.jsonl', unversioned)
      _await_published(target)
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(target))
    with pytest.raises(usage.UsageUnavailable, match='names no Claude Code version'):
      usage.current_usage()

  @pytest.mark.skipif(_ROOT, reason='root writes through a read-only directory')
  @pytest.mark.parametrize('billed_before', [True, False], ids=['after-a-snapshot', 'before-any'])
  def test_a_failure_it_cannot_record_fails_the_reads_at_once_and_raises_as_it_ends(
    self, tmp_path, monkeypatch, billed_before
  ):
    projects = tmp_path / 'projects'
    projects.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    target = state / 'usage.json'
    segment = projects / f'{SEGMENT}.jsonl'
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(target))
    with (
      _restoring_modes(state),
      pytest.raises(PermissionError),
      publishing_usage(projects, target),
    ):
      if billed_before:
        _append(segment, _assistant('m1', output=7))
        _await_published(target)
      state.chmod(0o500)
      _append(segment, _assistant('m2', output=3))
      deadline = time.monotonic() + 10
      while True:
        try:
          usage.current_usage()
        except usage.UsageUnavailable:
          break
        assert time.monotonic() < deadline, 'the stale snapshot stayed readable'
        time.sleep(0.05)

  def test_replaces_a_previous_lifetimes_snapshot_before_it_starts(self, tmp_path):
    projects = tmp_path / 'projects'
    projects.mkdir()
    target = tmp_path / 'usage.json'
    usage.write_usage_file(target, Usage(agent='Claude Code 2.1.1', per_model={OPUS: C(output=1)}))
    with publishing_usage(projects, target):
      assert usage.read_usage_file(target) is None
