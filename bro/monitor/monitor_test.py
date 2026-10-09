from bro import monitor


class TestSessionDir:
  def test_names_the_dir_the_session_declares(self, tmp_path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    assert monitor.session_dir() == tmp_path / 'session'
    assert monitor.harness_session_dir('claude') == tmp_path / 'session' / 'claude'

  def test_outside_a_managed_session_there_is_none(self, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    assert monitor.session_dir() is None
    assert monitor.harness_session_dir('claude') is None

  def test_the_workspace_placement_is_a_record_beside_the_tree(self, tmp_path):
    assert monitor.workspace_session_dir(tmp_path / 'ws') == tmp_path / 'ws' / 'session'
