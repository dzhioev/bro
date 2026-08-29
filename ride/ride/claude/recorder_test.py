import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bro.monitor import encode_project_path, health
from bro.trails.local import LocalStore
from bro.workspace.paths import trails_dir
from ride.claude.recorder import (
  _STOP_TIMEOUT,
  RECORDER_COMMAND,
  _SessionRecorder,
  start_session_recorder,
)


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
    argv = popen.call_args.args[0]
    assert argv[argv.index('--workspace') + 1] == 'w'
    assert argv[argv.index('--projects-dir') + 1] == str(projects_dir)
    assert argv[argv.index('--llm') + 1] == '{"model": "m"}'
    assert popen.call_args.kwargs['env'] == {'RIDE_WORKSPACE': 'w'}
    assert recorder.log_path == session_dir / 'claude' / 'session-recorder.log'

  def test_the_daemon_is_named_by_its_path_in_this_installation(self, tmp_path, monkeypatch):
    _, popen, _, _ = self._start(tmp_path, monkeypatch)
    daemon = Path(popen.call_args.args[0][0])
    assert daemon.name == RECORDER_COMMAND
    assert daemon.is_absolute()
    assert daemon.is_file()

  def test_an_unstartable_daemon_ends_the_launch(self, tmp_path, monkeypatch):
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    with (
      patch('ride.claude.recorder.claude_projects_dir', return_value=tmp_path / 'p'),
      patch('ride.claude.recorder.spawn.popen', side_effect=OSError('no such command')),
      pytest.raises(RuntimeError, match='cannot start the session recorder'),
    ):
      start_session_recorder('w', tmp_path / 'ws', {}, llm={})

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
    process.wait.side_effect = [subprocess.TimeoutExpired(RECORDER_COMMAND, 1), None]
    _SessionRecorder(process, tmp_path / 'recorder.log').stop()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def _record(**fields: Any) -> str:
  return json.dumps({'version': '2.1.216', 'timestamp': '2026-01-01T00:00:00.000Z', **fields})


_TRANSCRIPT = [
  _record(type='user', uuid='u1', message={'content': 'hello'}),
  _record(
    type='assistant',
    uuid='a1',
    message={
      'id': 'm1',
      'model': 'claude-fable-5',
      'usage': {'input_tokens': 1, 'output_tokens': 2},
      'content': [{'type': 'text', 'text': 'hi'}],
    },
  ),
]

# generous: the daemon is a real process importing the whole trails stack before
# its first tick, and the suite runs its tests in parallel
_RECORDING_TIMEOUT = 90.0


def _await_trail(store: LocalStore, segment: Path) -> dict:
  """the header of the trail the live daemon opens over `segment`.

  the daemon only adopts transcripts modified after it started, and it decides
  that instant itself — after its own imports — so the segment's mtime is kept
  current until the trail appears."""
  deadline = time.monotonic() + _RECORDING_TIMEOUT
  while True:
    headers = list(store.iter_trails(harness='claude'))
    if len(headers) > 0:
      return headers[0]
    if time.monotonic() >= deadline:
      raise AssertionError(f'no trail recorded within {_RECORDING_TIMEOUT:.0f}s')
    os.utime(segment)
    time.sleep(0.2)


class TestLiveRecording:
  """the daemon spawned for real, against a local trails store: it must resolve
  from this installation and record what claude writes. The session PATH the
  daemon inherits carries no framework command, as a managed session's does not
  either — the suite's own PATH would hide a daemon named by bare name."""

  def test_a_started_session_records_its_transcript(self, tmp_path, monkeypatch):
    claude_dir = tmp_path / 'claude'
    workspace = tmp_path / 'ws'
    projects = claude_dir / 'projects' / encode_project_path(workspace)
    projects.mkdir(parents=True)
    # an empty store leaves `trails` unresolvable, which selects local storage
    credential_store = tmp_path / 'credentials'
    credential_store.mkdir()
    (credential_store / 'creds.json').write_text('{}')
    monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(claude_dir))
    monkeypatch.setenv('BRO_STORE', str(credential_store))
    monkeypatch.setenv('RIDE_COMMAND', 'ride along ws')
    segment = projects / 'seg-1.jsonl'
    segment.write_text('\n'.join(_TRANSCRIPT) + '\n')
    session_env = {**os.environ, 'PATH': str(tmp_path / 'empty')}

    store = LocalStore(trails_dir())
    with contextlib.ExitStack() as running:
      recorder = start_session_recorder('ws', workspace, session_env, llm={'model': 'm'})
      running.callback(recorder.stop)
      header = _await_trail(store, segment)

    assert header['native']['segment'] == 'seg-1'
    assert header['native']['ride_command'] == 'ride along ws'
    assert header['native']['llm'] == {'model': 'm'}
    assert [step['body'] for step in store.iter_steps(header['id'])] == _TRANSCRIPT
    assert store.get_trail(header['id'])['end']['reason'] == 'ok'
    assert health.problem() is None
