import io
import json
import time

from bro.cw import statusline
from bro.monitor import health


def _run(monkeypatch, tmp_path, status=None, summon_status=None) -> str:
  monkeypatch.setattr(health, 'health_path', lambda: tmp_path / 'health.json')
  if status is not None:
    health.write(status, interval=3)
  monkeypatch.delenv(statusline.STATUS_ENV, raising=False)
  if summon_status is not None:
    summon_file = tmp_path / 'summon-status.json'
    if isinstance(summon_status, str):
      summon_file.write_text(summon_status)
    else:
      summon_file.write_text(json.dumps(summon_status))
    monkeypatch.setenv(statusline.STATUS_ENV, str(summon_file))
  monkeypatch.setattr('sys.stdin', io.StringIO('{"cwd":"/workspace"}'))
  out = io.StringIO()
  monkeypatch.setattr('sys.stdout', out)
  assert statusline.statusline() == 0
  return out.getvalue()


class TestStatusline:
  def test_failing_prints_red_warning(self, monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, status='error')
    assert '⚠ session recording FAILING' in out
    assert '\033[1;31m' in out

  def test_ok_prints_nothing(self, monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, status='ok').strip() == ''

  def test_absent_prints_nothing(self, monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path).strip() == ''


class TestSummonSection:
  def test_active_summon_shows_target_trail_and_age(self, monkeypatch, tmp_path):
    summon_status = {
      'active': [
        {
          'request_id': 'R1',
          'target': 'reviewer',
          'trail_id': 'T1',
          'started_at': time.time() - 185,
        }
      ],
      'last': None,
    }
    out = _run(monkeypatch, tmp_path, summon_status=summon_status)
    assert '⚡ summoning reviewer 3m (trail T1)' in out

  def test_active_summon_without_a_trail_yet(self, monkeypatch, tmp_path):
    summon_status = {
      'active': [
        {'request_id': 'R1', 'target': 'reviewer', 'trail_id': None, 'started_at': time.time() - 5}
      ],
      'last': None,
    }
    out = _run(monkeypatch, tmp_path, summon_status=summon_status)
    assert '⚡ summoning reviewer 5s (no trail yet)' in out

  def test_recent_terminal_outcome_shows(self, monkeypatch, tmp_path):
    summon_status = {
      'active': [],
      'last': {
        'target': 'reviewer',
        'trail_id': 'T1',
        'outcome': 'ok',
        'ended_at': time.time() - 30,
      },
    }
    out = _run(monkeypatch, tmp_path, summon_status=summon_status)
    assert '✓ summon reviewer: ok' in out

  def test_failed_outcome_is_marked_as_failure(self, monkeypatch, tmp_path):
    summon_status = {
      'active': [],
      'last': {
        'target': 'reviewer',
        'trail_id': None,
        'outcome': 'failed:timeout',
        'ended_at': time.time() - 30,
      },
    }
    out = _run(monkeypatch, tmp_path, summon_status=summon_status)
    assert '✗ summon reviewer: failed:timeout' in out

  def test_stale_outcome_drops_off(self, monkeypatch, tmp_path):
    summon_status = {
      'active': [],
      'last': {
        'target': 'reviewer',
        'trail_id': 'T1',
        'outcome': 'ok',
        'ended_at': time.time() - 2000,
      },
    }
    assert _run(monkeypatch, tmp_path, summon_status=summon_status).strip() == ''

  def test_unreadable_status_file_is_reported(self, monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, summon_status='not json{')
    assert '⚠ summon status unreadable' in out

  def test_missing_file_prints_nothing(self, monkeypatch, tmp_path):
    monkeypatch.setenv(statusline.STATUS_ENV, str(tmp_path / 'nope.json'))
    monkeypatch.setattr(health, 'health_path', lambda: tmp_path / 'health.json')
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    out = io.StringIO()
    monkeypatch.setattr('sys.stdout', out)
    assert statusline.statusline() == 0
    assert out.getvalue().strip() == ''

  def test_sections_join_on_one_line(self, monkeypatch, tmp_path):
    summon_status = {
      'active': [
        {'request_id': 'R1', 'target': 'reviewer', 'trail_id': 'T1', 'started_at': time.time()}
      ],
      'last': None,
    }
    out = _run(monkeypatch, tmp_path, status='error', summon_status=summon_status)
    assert out.count('\n') == 1  # claude renders a single status line
    assert '⚠ session recording FAILING' in out
    assert '⚡ summoning reviewer' in out
    assert ' · ' in out
