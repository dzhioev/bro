import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ride.claude.recorder import _STOP_TIMEOUT, _SessionRecorder, start_session_recorder


class TestStart:
  def _start(self, tmp_path, monkeypatch, **kwargs):
    session_dir = tmp_path / 'session'
    projects_dir = tmp_path / 'config' / 'projects' / '-ws'
    monkeypatch.setenv('RIDE_SESSION_DIR', str(session_dir))
    with (
      patch('ride.claude.recorder.claude_projects_dir', return_value=projects_dir),
      patch('ride.claude.recorder.spawn.popen') as popen,
    ):
      recorder = start_session_recorder(
        'w', tmp_path / 'ws', {'RIDE_WORKSPACE': 'w'}, llm=kwargs.pop('llm', {'model': 'm'})
      )
    return recorder, popen, session_dir, projects_dir

  def test_spawns_the_daemon_on_the_session_paths(self, tmp_path, monkeypatch):
    recorder, popen, session_dir, projects_dir = self._start(tmp_path, monkeypatch)
    assert recorder is not None
    argv = popen.call_args.args[0]
    assert argv[0] == 'bro.trails.record.claude'
    assert argv[argv.index('--workspace') + 1] == 'w'
    assert argv[argv.index('--projects-dir') + 1] == str(projects_dir)
    assert argv[argv.index('--llm') + 1] == '{"model": "m"}'
    assert popen.call_args.kwargs['env'] == {'RIDE_WORKSPACE': 'w'}
    assert recorder.log_path == session_dir / 'claude' / 'session-recorder.log'

  def test_spawn_failure_returns_none_and_beats_the_session_health_file(
    self, tmp_path, monkeypatch
  ):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    with (
      patch('ride.claude.recorder.claude_projects_dir', return_value=tmp_path / 'p'),
      patch('ride.claude.recorder.spawn.popen', side_effect=OSError('no such command')),
    ):
      assert start_session_recorder('w', tmp_path / 'ws', {}, llm={}) is None
    recorded = json.loads((tmp_path / 'session' / 'session-recorder-health.json').read_text())
    assert recorded['status'] == 'error'

  def test_outside_a_session_the_wiring_is_a_bug(self, tmp_path, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    with (
      patch('ride.claude.recorder.claude_projects_dir', return_value=tmp_path / 'p'),
      pytest.raises(RuntimeError, match='RIDE_SESSION_DIR'),
    ):
      start_session_recorder('w', tmp_path / 'ws', {}, llm={})


class TestStop:
  def test_terminates_and_waits_for_the_final_snapshot(self, tmp_path):
    process = MagicMock()
    _SessionRecorder(process, tmp_path / 'recorder.log').stop()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=_STOP_TIMEOUT)
    process.kill.assert_not_called()

  def test_kills_when_the_final_snapshot_hangs(self, tmp_path):
    process = MagicMock()
    process.wait.side_effect = [subprocess.TimeoutExpired('bro.trails.record.claude', 1), None]
    _SessionRecorder(process, tmp_path / 'recorder.log').stop()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
