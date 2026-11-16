import json
import os
import subprocess
import sys
from unittest.mock import Mock, call

import pytest

from bro.monitor import health, trail_pointer
from bro.native import recorder
from bro.native.recorder import cleared_pointer, session_recorder
from bro.native.run_log_test_helper import open_log, session_environment
from bro.trails.store import default_store


@pytest.mark.parametrize('opened', [False, True])
def test_pointer_lifetime(tmp_path, opened):
  pointer = tmp_path / 'pointer.json'
  trail_pointer.write(pointer, 'previous')
  original = pointer.read_bytes()
  path = tmp_path / 'run.jsonl'
  with cleared_pointer(path, pointer):
    assert not pointer.exists()
    if opened:
      path.touch()
      trail_pointer.write(pointer, 'current')
  assert pointer.read_bytes() == (
    json.dumps({'trail_id': 'current'}).encode() if opened else original
  )


def test_absent_pointer_is_not_invented(tmp_path):
  pointer = tmp_path / 'absent'
  with cleared_pointer(tmp_path / 'run.jsonl', pointer):
    pass
  assert not pointer.exists()


def test_pointer_removal_failure_refuses_entry(tmp_path):
  pointer = tmp_path / 'not-a-file'
  pointer.mkdir()
  with pytest.raises(IsADirectoryError), cleared_pointer(tmp_path / 'run.jsonl', pointer):
    pytest.fail('entered with an uncleared pointer')


def test_pointer_restored_after_launch_failure(tmp_path):
  pointer = tmp_path / 'pointer.json'
  trail_pointer.write(pointer, 'previous')
  with pytest.raises(RuntimeError), cleared_pointer(tmp_path / 'run.jsonl', pointer):
    raise RuntimeError('launch failed')
  assert trail_pointer.read(pointer) == 'previous'


def test_recorder_exiting_without_readiness_refuses_launch(tmp_path, monkeypatch):
  executable = tmp_path / 'early-exit'
  executable.write_text(f'#!{sys.executable}\nprint("early failure")\n')
  executable.chmod(0o700)
  monkeypatch.setattr(recorder.spawn, 'console_script', lambda name: str(executable))
  log_path = tmp_path / 'recorder.log'
  with pytest.raises(RuntimeError, match=f'before readiness.*{log_path}'):
    with session_recorder(tmp_path / 'run.jsonl', log_path, os.environ):
      pytest.fail('launched without readiness')
  assert 'early failure' in log_path.read_text()


def test_recorder_start_error_names_log(tmp_path, monkeypatch):
  monkeypatch.setattr(recorder.spawn, 'console_script', lambda name: '/missing/recorder')
  log_path = tmp_path / 'recorder.log'
  with pytest.raises(RuntimeError, match=f'cannot start.*{log_path}'):
    with session_recorder(tmp_path / 'run.jsonl', log_path, os.environ):
      pytest.fail('launched without recorder')


def test_real_lifetime_stops_after_the_log_and_preserves_pointer(tmp_path, monkeypatch):
  environment = session_environment(tmp_path)
  for key, value in environment.items():
    monkeypatch.setenv(key, value)
  path = tmp_path / 'runs' / 'run.jsonl'
  log_path = tmp_path / 'recorder.log'
  with cleared_pointer(path):
    with session_recorder(path, log_path, environment) as process:
      assert os.getsid(process.pid) == process.pid
      writer = open_log(path)
      writer.step('user_input', 'whole log')
      writer.end('ok')
    assert process.returncode == 0
    pointer = trail_pointer.path()
    assert pointer is not None
    identifier = trail_pointer.read(pointer)
    assert identifier is not None
    with default_store() as store:
      assert len(list(store.iter_steps(identifier))) == 2
      assert store.get_trail(identifier)['end']['reason'] == 'ok'
  assert trail_pointer.read(pointer) == identifier
  health_path = health.health_path()
  assert health_path is not None
  assert json.loads(health_path.read_text())['stale_after'] is None


def test_stop_timeout_kills_and_names_log(tmp_path, caplog):
  process = Mock(spec=subprocess.Popen)
  process.wait.side_effect = [subprocess.TimeoutExpired('recorder', recorder._STOP_TIMEOUT), 0]
  log_path = tmp_path / 'recorder.log'
  recorder._stop(process, log_path)
  assert process.mock_calls == [
    call.terminate(),
    call.wait(timeout=recorder._STOP_TIMEOUT),
    call.kill(),
    call.wait(),
  ]
  assert str(log_path) in caplog.text


def test_pointer_restoration_follows_recorder_exit_when_run_never_opens(tmp_path, monkeypatch):
  environment = session_environment(tmp_path)
  for key, value in environment.items():
    monkeypatch.setenv(key, value)
  pointer = trail_pointer.path()
  assert pointer is not None
  trail_pointer.write(pointer, 'previous')
  path = tmp_path / 'runs' / 'unopened.jsonl'
  with cleared_pointer(path):
    with session_recorder(path, tmp_path / 'recorder.log', environment) as process:
      assert not pointer.exists()
    assert process.returncode == 0
    assert not pointer.exists()
  assert trail_pointer.read(pointer) == 'previous'
