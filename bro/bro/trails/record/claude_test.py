from pathlib import Path
from unittest.mock import MagicMock

import session_log_health
import sync_session_log


def _redirect_health(monkeypatch, tmp_path) -> Path:
  path = tmp_path / 'health.json'
  monkeypatch.setattr(session_log_health, 'HEALTH_PATH', path)
  return path


def _stub_backend(monkeypatch, tmp_path):
  # make the one-shot sync_session_log path reach _sync_once without real AWS
  monkeypatch.setattr(sync_session_log, '_load_config', lambda: {'bucket': 'b', 'table': 't'})
  monkeypatch.setattr(sync_session_log, '_create_session', lambda config: MagicMock())
  monkeypatch.setattr(sync_session_log, '_latest_jsonl', lambda d: tmp_path / 'log.jsonl')


class TestHealthOnOneShot:
  def test_success_writes_ok(self, monkeypatch, tmp_path):
    _redirect_health(monkeypatch, tmp_path)
    _stub_backend(monkeypatch, tmp_path)
    monkeypatch.setattr(sync_session_log, '_sync_once', lambda *a, **k: None)
    assert sync_session_log.sync_session_log(workspace='ws') == 0
    assert session_log_health.is_failing() is False

  def test_failure_writes_error_and_reraises(self, monkeypatch, tmp_path):
    _redirect_health(monkeypatch, tmp_path)
    _stub_backend(monkeypatch, tmp_path)

    def boom(*a, **k):
      raise RuntimeError('AccessDeniedException: PutItem')

    monkeypatch.setattr(sync_session_log, '_sync_once', boom)
    try:
      sync_session_log.sync_session_log(workspace='ws')
      raised = False
    except RuntimeError:
      raised = True
    assert raised
    assert session_log_health.is_failing() is True

  def test_missing_config_writes_error(self, monkeypatch, tmp_path):
    from base import credentials

    _redirect_health(monkeypatch, tmp_path)

    def _missing():
      raise credentials.SecretNotFound('session_log')

    monkeypatch.setattr(sync_session_log, '_load_config', _missing)
    assert sync_session_log.sync_session_log(workspace='ws') == 1
    assert session_log_health.is_failing() is True
