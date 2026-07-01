import json
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


def _write_log(path: Path) -> None:
  path.write_text(
    '\n'.join(
      json.dumps(e)
      for e in [
        {
          'type': 'user',
          'timestamp': '2026-07-01T10:00:00Z',
          'version': '2.1.195',
          'message': {'content': 'hello'},
        },
        {
          'type': 'assistant',
          'version': '2.1.195',
          'message': {'model': 'claude-opus-4-8', 'content': [{'type': 'text', 'text': 'hi'}]},
        },
      ]
    )
  )


class TestMetadata:
  def test_extracts_claude_code_version(self, tmp_path):
    log_path = tmp_path / 'log.jsonl'
    _write_log(log_path)
    meta = sync_session_log._extract_metadata(log_path)
    assert meta['version'] == '2.1.195'
    assert meta['model'] == 'claude-opus-4-8'


class TestBuildItem:
  def test_version_and_context_into_item(self, monkeypatch, tmp_path):
    log_path = tmp_path / 'sid.jsonl'
    _write_log(log_path)
    records = [{'kind': 'git', 'subtype': 'state', 'title': 'git', 'fields': {'branch': 'b'}}]
    monkeypatch.setenv('CW_SESSION_CONTEXT', json.dumps(records))
    item = sync_session_log._build_item(log_path, 'ws', 'logs/ws/sid.jsonl')
    assert item['claude_code_version'] == '2.1.195'
    assert json.loads(item['context']) == records

  def test_context_absent_when_env_unset(self, monkeypatch, tmp_path):
    log_path = tmp_path / 'sid.jsonl'
    _write_log(log_path)
    monkeypatch.delenv('CW_SESSION_CONTEXT', raising=False)
    item = sync_session_log._build_item(log_path, 'ws', 'logs/ws/sid.jsonl')
    assert 'context' not in item


class TestProjectsDirOverride:
  def test_override_is_read_instead_of_default(self, monkeypatch, tmp_path):
    # the host-side --bro sync passes the container's bind-mounted transcript dir
    # explicitly; it must win over the cwd-derived _projects_dir() default
    _redirect_health(monkeypatch, tmp_path)
    monkeypatch.setattr(sync_session_log, '_load_config', lambda: {'bucket': 'b', 'table': 't'})
    monkeypatch.setattr(sync_session_log, '_create_session', lambda config: MagicMock())
    monkeypatch.setattr(sync_session_log, '_sync_once', lambda *a, **k: None)

    def _no_default():
      raise AssertionError('default _projects_dir must not be consulted')

    seen = []

    def _record(d):
      seen.append(d)
      return tmp_path / 'log.jsonl'

    monkeypatch.setattr(sync_session_log, '_projects_dir', _no_default)
    monkeypatch.setattr(sync_session_log, '_latest_jsonl', _record)
    override = tmp_path / 'custom-projects'
    assert sync_session_log.sync_session_log(workspace='ws', projects_dir=override) == 0
    assert seen == [override]
