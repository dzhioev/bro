import datetime
import json

from bro.monitor import health


def _redirect(monkeypatch, tmp_path):
  path = tmp_path / 'health.json'
  monkeypatch.setattr(health, 'health_path', lambda: path)
  return path


def _age_beat(path, seconds: float) -> None:
  """backdate the recorded beat, as a writer that stopped beating would leave it."""
  data = json.loads(path.read_text())
  beat = datetime.datetime.fromisoformat(data['checked_at'])
  data['checked_at'] = (beat - datetime.timedelta(seconds=seconds)).isoformat()
  path.write_text(json.dumps(data))


class TestHealth:
  def test_a_fresh_beat_is_silent(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    health.write('ok', interval=3)
    assert health.problem() is None

  def test_error_reports_failing(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    health.write('error', 'AccessDeniedException: nope', interval=3)
    assert health.problem() == health._FAILING

  def test_a_later_beat_clears_an_error(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    health.write('error', 'AccessDeniedException: nope', interval=3)
    health.write('ok', interval=3)
    assert health.problem() is None

  def test_a_missed_beat_reports_stopped(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('ok', interval=3)
    _age_beat(path, health._BEAT_GRACE + 60)
    assert health.problem() == health._STOPPED

  def test_a_slow_attempt_stays_within_the_grace(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('ok', interval=3)
    _age_beat(path, health._BEAT_GRACE - 10)
    assert health.problem() is None

  def test_a_missed_beat_keeps_the_recorded_error(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('error', 'AccessDeniedException: nope', interval=3)
    _age_beat(path, health._BEAT_GRACE + 60)
    assert health.problem() == health._FAILING

  def test_a_final_write_never_goes_stale(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('ok', interval=None)
    _age_beat(path, 100_000)
    assert health.problem() is None

  def test_error_is_trimmed(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('error', 'x' * 5000, interval=3)
    assert len(json.loads(path.read_text())['error']) == health._MAX_ERROR

  def test_write_is_atomic_no_tmp_left(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    health.write('ok', interval=3)
    assert list(path.parent.iterdir()) == [path]

  def test_absent_is_silent(self, monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    assert health.problem() is None

  def test_garbage_is_silent(self, monkeypatch, tmp_path):
    path = _redirect(monkeypatch, tmp_path)
    path.write_text('{not json')
    assert health.problem() is None


class TestHealthPath:
  def test_follows_the_session_config_dir(self, monkeypatch, tmp_path):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'session-config'))
    assert health.health_path() == (tmp_path / 'session-config' / 'session-recorder-health.json')

  def test_defaults_to_the_home_claude_dir(self, monkeypatch):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    assert health.health_path().parent.name == '.claude'
