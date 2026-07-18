import os
import signal

import pytest

import workspace.session


class TestTerminateSession:
  def test_signals_the_recorded_runner(self, monkeypatch):
    kills: list[tuple[int, int]] = []
    monkeypatch.setenv('CW_RUNNER_PID', '4242')
    monkeypatch.setattr(os, 'kill', lambda pid, sig: kills.append((pid, sig)))
    workspace.session.terminate_session()
    assert kills == [(4242, signal.SIGTERM)]

  def test_fails_without_a_runner_pid(self, monkeypatch):
    monkeypatch.delenv('CW_RUNNER_PID', raising=False)
    with pytest.raises(KeyError):
      workspace.session.terminate_session()
