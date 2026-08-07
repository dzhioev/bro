import json
from pathlib import Path

from bro.monitor import trail_pointer


class TestPath:
  def test_derives_from_claude_config_dir(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    assert trail_pointer.path() == tmp_path / 'current-trail.json'

  def test_defaults_to_home_claude(self, tmp_path: Path, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    assert trail_pointer.path() == tmp_path / '.claude' / 'current-trail.json'


class TestPublish:
  def test_roundtrips_through_read(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'config'))
    trail_pointer.publish('T1')
    assert trail_pointer.read(tmp_path / 'config' / 'current-trail.json') == 'T1'

  def test_overwrites_the_previous_pointer(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    trail_pointer.publish('T1')
    trail_pointer.publish('T2')
    assert trail_pointer.read(trail_pointer.path()) == 'T2'

  def test_clear_removes_the_file(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    trail_pointer.publish('T1')
    trail_pointer.clear()
    assert not trail_pointer.path().exists()

  def test_clear_without_a_pointer_is_a_noop(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    trail_pointer.clear()


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
