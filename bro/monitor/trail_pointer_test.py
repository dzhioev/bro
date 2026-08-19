import json
from pathlib import Path

from bro.monitor import trail_pointer


class TestPath:
  def test_derives_from_the_session_state_dir(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    assert trail_pointer.path() == tmp_path / 'current-trail.json'

  def test_outside_a_session_there_is_none(self, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    assert trail_pointer.path() is None

  def test_the_workspace_placement_names_the_same_file(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'ws' / 'session'))
    assert trail_pointer.session_pointer(tmp_path / 'ws') == trail_pointer.path()


class TestPublish:
  def test_roundtrips_through_read(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    trail_pointer.publish('T1')
    assert trail_pointer.read(tmp_path / 'session' / 'current-trail.json') == 'T1'

  def test_overwrites_the_previous_pointer(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    trail_pointer.publish('T1')
    trail_pointer.publish('T2')
    assert trail_pointer.read(tmp_path / 'current-trail.json') == 'T2'

  def test_clear_removes_the_file(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    trail_pointer.publish('T1')
    trail_pointer.clear()
    assert not (tmp_path / 'current-trail.json').exists()

  def test_clear_without_a_pointer_is_a_noop(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path))
    trail_pointer.clear()

  def test_outside_a_session_publishing_writes_nothing(self, tmp_path: Path, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    monkeypatch.chdir(tmp_path)
    trail_pointer.publish('T1')
    trail_pointer.clear()
    assert list(tmp_path.iterdir()) == []


class TestRead:
  def test_absent_file_reads_none(self, tmp_path: Path):
    assert trail_pointer.read(tmp_path / 'current-trail.json') is None

  def test_unparsable_file_reads_none(self, tmp_path: Path):
    pointer = tmp_path / 'current-trail.json'
    pointer.write_text('not json')
    assert trail_pointer.read(pointer) is None

  def test_missing_or_empty_trail_id_reads_none(self, tmp_path: Path):
    pointer = tmp_path / 'current-trail.json'
    pointer.write_text(json.dumps({}))
    assert trail_pointer.read(pointer) is None
    pointer.write_text(json.dumps({'trail_id': ''}))
    assert trail_pointer.read(pointer) is None
