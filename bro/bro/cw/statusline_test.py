import io

import session_log_health
import session_log_statusline


def _run(monkeypatch, tmp_path, status=None) -> str:
  monkeypatch.setattr(session_log_health, 'HEALTH_PATH', tmp_path / 'health.json')
  if status is not None:
    session_log_health.write(status)
  monkeypatch.setattr('sys.stdin', io.StringIO('{"cwd":"/workspace"}'))
  out = io.StringIO()
  monkeypatch.setattr('sys.stdout', out)
  assert session_log_statusline.statusline() == 0
  return out.getvalue()


class TestStatusline:
  def test_failing_prints_red_warning(self, monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, status='error')
    assert '⚠ session-log sync FAILING' in out
    assert '\033[1;31m' in out

  def test_ok_prints_nothing(self, monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, status='ok').strip() == ''

  def test_absent_prints_nothing(self, monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path).strip() == ''
