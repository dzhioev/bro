import subprocess
from unittest.mock import MagicMock, patch

from cw.session_log import _STOP_TIMEOUT, _SessionLogSync, _start_session_log_sync


class TestStart:
  def _start(self, tmp_path, **kwargs):
    config_dir = tmp_path / 'config'
    projects_dir = tmp_path / 'config' / 'projects' / '-ws'
    with (
      patch('cw.session_log._claude_config_dir', return_value=config_dir),
      patch('cw.session_log._claude_projects_dir', return_value=projects_dir),
      patch('cw.session_log.spawn.popen') as popen,
    ):
      sync = _start_session_log_sync('w', tmp_path / 'ws', {'CW_NAME': 'w'}, **kwargs)
    return sync, popen, config_dir, projects_dir

  def test_spawns_the_daemon_on_the_session_paths(self, tmp_path):
    sync, popen, config_dir, projects_dir = self._start(tmp_path)
    assert sync is not None
    argv = popen.call_args.args[0]
    assert argv[:2] == ['sync-session-log', '--watch']
    assert argv[argv.index('--workspace') + 1] == 'w'
    assert argv[argv.index('--projects-dir') + 1] == str(projects_dir)
    assert '--resume-segment' not in argv
    assert popen.call_args.kwargs['env'] == {'CW_NAME': 'w'}
    assert sync.log_path == config_dir / 'session-log-sync.log'

  def test_forwards_the_resumed_segment(self, tmp_path):
    sync, popen, _, _ = self._start(tmp_path, resume_segment='abc')
    assert sync is not None
    argv = popen.call_args.args[0]
    assert argv[argv.index('--resume-segment') + 1] == 'abc'

  def test_spawn_failure_returns_none(self, tmp_path):
    config_dir = tmp_path / 'config'
    with (
      patch('cw.session_log._claude_config_dir', return_value=config_dir),
      patch('cw.session_log._claude_projects_dir', return_value=tmp_path / 'p'),
      patch('cw.session_log.spawn.popen', side_effect=OSError('no such command')),
    ):
      assert _start_session_log_sync('w', tmp_path / 'ws', {}) is None


class TestStop:
  def test_terminates_and_waits_for_the_final_sync(self, tmp_path):
    process = MagicMock()
    _SessionLogSync(process, tmp_path / 'sync.log').stop()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=_STOP_TIMEOUT)
    process.kill.assert_not_called()

  def test_kills_when_the_final_sync_hangs(self, tmp_path):
    process = MagicMock()
    process.wait.side_effect = [subprocess.TimeoutExpired('sync-session-log', 1), None]
    _SessionLogSync(process, tmp_path / 'sync.log').stop()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
