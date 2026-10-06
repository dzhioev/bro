from unittest.mock import MagicMock

import pytest

import ride.claude.harness as claude_harness
import ride.harness as ride_harness
from bro.harness import Harness


def test_get_harness_refuses_an_object_without_the_session_interface(monkeypatch):
  monkeypatch.setattr(ride_harness, 'load_harness', lambda _name: Harness('identity-only'))

  with pytest.raises(TypeError, match='does not implement ride SessionHarness'):
    ride_harness.get_harness('identity-only')


def test_claude_runtime_check_starts_the_carried_binary(monkeypatch, tmp_path):
  prefix = tmp_path / 'bundle' / 'venv'
  binary = tmp_path / 'bundle' / 'claude' / 'claude'
  binary.parent.mkdir(parents=True)
  binary.touch()
  run = MagicMock()
  monkeypatch.setattr(claude_harness.sys, 'prefix', str(prefix))
  monkeypatch.setattr(claude_harness.claude_release, 'verified_binary', lambda path: path)
  monkeypatch.setattr(claude_harness.subprocess, 'run', run)

  claude_harness.CLAUDE.check_runtime()

  run.assert_called_once_with([str(binary), '--version'], check=True)
