import time
from pathlib import Path

from ride.claude.transcripts import Changes, read_lines_after, subagent_transcripts, watching


class TestSubagentTranscripts:
  def test_lists_the_transcripts_under_the_segments_companion_dir(self, tmp_path):
    segment = tmp_path / 'session.jsonl'
    segment.touch()
    subagents = tmp_path / 'session' / 'subagents'
    subagents.mkdir(parents=True)
    (subagents / 'agent-b.jsonl').touch()
    (subagents / 'agent-a.jsonl').touch()
    (tmp_path / 'session' / 'tool-results').mkdir()
    (tmp_path / 'session' / 'tool-results' / 'output.txt').touch()
    (tmp_path / 'other.jsonl').touch()
    assert subagent_transcripts(segment) == [
      subagents / 'agent-a.jsonl',
      subagents / 'agent-b.jsonl',
    ]

  def test_a_session_without_subagents_has_none(self, tmp_path):
    segment = tmp_path / 'session.jsonl'
    segment.touch()
    assert subagent_transcripts(segment) == []


class TestReadLinesAfter:
  def test_reads_complete_lines_and_leaves_a_partial_one(self, tmp_path):
    path = tmp_path / 't.jsonl'
    path.write_text('one\ntwo\nthr')
    lines, offset = read_lines_after(path, 0)
    assert lines == ['one', 'two']
    with path.open('a') as stream:
      stream.write('ee\n')
    assert read_lines_after(path, offset) == (['three'], path.stat().st_size)

  def test_nothing_new_keeps_the_offset(self, tmp_path):
    path = tmp_path / 't.jsonl'
    path.write_text('one\n')
    assert read_lines_after(path, 4) == ([], 4)


def _write_until_woken(changes: Changes, transcript: Path) -> None:
  """append to `transcript` until a write wakes `changes`: the watch registers in
  its own thread, so a first write may precede it."""
  transcript.parent.mkdir(parents=True, exist_ok=True)
  deadline = time.monotonic() + 10
  while True:
    with transcript.open('a') as stream:
      stream.write('{}\n')
    if changes.wait(0.2):
      return
    assert time.monotonic() < deadline, 'no transcript write woke the reader'


class TestWatching:
  def test_a_subagent_transcript_write_wakes_the_reader(self, tmp_path):
    projects = tmp_path / 'projects'
    with watching(projects) as changes:
      _write_until_woken(changes, projects / 'session' / 'subagents' / 'agent-a.jsonl')

  def test_a_write_to_another_file_does_not(self, tmp_path):
    projects = tmp_path / 'projects'
    with watching(projects) as changes:
      _write_until_woken(changes, projects / 'session.jsonl')
      (projects / 'session' / 'tool-results').mkdir(parents=True)
      (projects / 'session' / 'tool-results' / 'output.txt').write_text('x')
      assert not changes.wait(1)

  def test_a_notify_wakes_the_reader(self, tmp_path):
    with watching(tmp_path / 'projects') as changes:
      changes.notify()
      assert changes.wait(10)
