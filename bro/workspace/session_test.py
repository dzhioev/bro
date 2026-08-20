import os
import signal

import pytest

import bro.workspace.session as workspace_session


@pytest.fixture
def session(monkeypatch, tmp_path):
  monkeypatch.setenv('RIDE_SESSION_DIR', str(tmp_path / 'session'))
  return tmp_path / 'session'


class TestTerminateSession:
  def test_signals_the_recorded_runner(self, monkeypatch, session):
    kills: list[tuple[int, int]] = []
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    workspace_session.terminate_session(0)
    assert kills == [(4242, signal.SIGTERM)]

  def test_leaves_the_status_the_session_is_to_report(self, monkeypatch, session):
    monkeypatch.setenv('RIDE_RUNNER_PID', '4242')
    monkeypatch.setattr(os, 'kill', lambda pid, sig: None)
    workspace_session.terminate_session(3)
    assert workspace_session.requested_exit_status() == 3

  def test_fails_without_a_runner_pid(self, monkeypatch, session):
    monkeypatch.delenv('RIDE_RUNNER_PID', raising=False)
    with pytest.raises(KeyError):
      workspace_session.terminate_session(0)

  def test_fails_outside_a_managed_session(self, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    with pytest.raises(RuntimeError):
      workspace_session.terminate_session(0)


class TestRequestedExitStatus:
  def test_none_when_nothing_asked(self, session):
    assert workspace_session.requested_exit_status() is None

  def test_none_outside_a_managed_session(self, monkeypatch):
    monkeypatch.delenv('RIDE_SESSION_DIR', raising=False)
    assert workspace_session.requested_exit_status() is None

  def test_cleared_so_only_this_sessions_request_is_read(self, session):
    session.mkdir(parents=True)
    (session / workspace_session.FILENAME).write_text('7')
    workspace_session.clear_requested_exit_status()
    assert workspace_session.requested_exit_status() is None
