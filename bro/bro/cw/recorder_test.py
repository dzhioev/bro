import subprocess
from unittest.mock import MagicMock, patch

from bro.cw.recorder import _STOP_TIMEOUT, _SessionRecorder, _start_session_recorder


class TestStart:
  def _start(self, tmp_path, **kwargs):
    config_dir = tmp_path / 'config'
    projects_dir = tmp_path / 'config' / 'projects' / '-ws'
    with (
      patch('bro.cw.recorder._claude_config_dir', return_value=config_dir),
      patch('bro.cw.recorder._claude_projects_dir', return_value=projects_dir),
      patch('bro.cw.recorder.spawn.popen') as popen,
    ):
      recorder = _start_session_recorder(
        'w', tmp_path / 'ws', {'CW_NAME': 'w'}, llm=kwargs.pop('llm', {'model': 'm'})
      )
    return recorder, popen, config_dir, projects_dir

  def test_spawns_the_daemon_on_the_session_paths(self, tmp_path):
    recorder, popen, config_dir, projects_dir = self._start(tmp_path)
    assert recorder is not None
    argv = popen.call_args.args[0]
    assert argv[0] == 'bro.trails.record.claude'
    assert argv[argv.index('--workspace') + 1] == 'w'
    assert argv[argv.index('--projects-dir') + 1] == str(projects_dir)
    assert argv[argv.index('--llm') + 1] == '{"model": "m"}'
    assert popen.call_args.kwargs['env'] == {'CW_NAME': 'w'}
    assert recorder.log_path == config_dir / 'session-recorder.log'

  def test_spawn_failure_returns_none(self, tmp_path):
    config_dir = tmp_path / 'config'
    with (
      patch('bro.cw.recorder._claude_config_dir', return_value=config_dir),
      patch('bro.cw.recorder._claude_projects_dir', return_value=tmp_path / 'p'),
      patch('bro.cw.recorder.spawn.popen', side_effect=OSError('no such command')),
    ):
      assert _start_session_recorder('w', tmp_path / 'ws', {}, llm={}) is None


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
