import json

from monitor import health


def _redirect(monkeypatch, tmp_path):
  path = tmp_path / 'health.json'
  monkeypatch.setattr(health, 'health_path', lambda: path)
  return path


class TestHealth:
  def test_ok_is_not_failing(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    health.write('ok')
    assert health.is_failing() is False

  def test_error_is_failing(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    health.write('error', 'AccessDeniedException: nope')
    assert health.is_failing() is True

  def test_error_is_trimmed(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('error', 'x' * 5000)
    assert len(json.loads(path.read_text())['error']) == health._MAX_ERROR

  def test_write_is_atomic_no_tmp_left(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('ok')
    assert list(path.parent.iterdir()) == [path]

  def test_absent_is_not_failing(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    assert health.is_failing() is False

  def test_garbage_is_not_failing(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    path.write_text('{not json')
    assert health.is_failing() is False


class TestHealthPath:
  def test_follows_the_session_config_dir(self, monkeypatch, tmp_path):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'session-config'))
    assert health.health_path() == (tmp_path / 'session-config' / 'session-recorder-health.json')

  def test_defaults_to_the_home_claude_dir(self, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    assert health.health_path().parent.name == '.claude'
