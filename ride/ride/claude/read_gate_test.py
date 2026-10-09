import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ride.claude.read_gate as read_gate
from bro import mcp
from ride.claude.claude_argv import gate_hooks

_SESSION = '0b6c3c8e-session'


class _Session:
  """a session's Claude folders as Claude Code lays them out, and a workspace
  file outside them."""

  def __init__(self, root: Path) -> None:
    project = root / 'config' / 'projects' / '-workspace'
    self.transcript = project / f'{_SESSION}.jsonl'
    self.temp_root = root / 'tmp'
    self.background = (
      self.temp_root / f'claude-{os.getuid()}' / '-workspace' / _SESSION / 'tasks' / 'b1.output'
    )
    self.oversized = project / _SESSION / 'tool-results' / 'r1.txt'
    self.workspace_file = root / 'workspace' / 'README.md'
    for path in (self.transcript, self.background, self.oversized, self.workspace_file):
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text('content\n')

  def payload(self, file_path: object) -> dict:
    return {
      'session_id': _SESSION,
      'transcript_path': str(self.transcript),
      'cwd': str(self.workspace_file.parent),
      'hook_event_name': 'PreToolUse',
      'tool_name': 'Read',
      'tool_input': {'file_path': file_path},
    }


@pytest.fixture
def session(tmp_path, monkeypatch) -> _Session:
  built = _Session(tmp_path)
  monkeypatch.setenv(read_gate.TEMP_ROOT_ENV, str(built.temp_root))
  return built


def _decide(monkeypatch, capsys, payload: dict) -> dict | None:
  """the gate's decision on `payload`: None where it admits the call."""
  monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
  assert read_gate.main(['read_gate']) == 0
  out = capsys.readouterr().out
  return None if out == '' else json.loads(out)['hookSpecificOutput']


def _link(path: Path, target: Path) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.symlink_to(target)
  return path


class TestResolution:
  @pytest.mark.parametrize('kept', ['background', 'oversized'])
  def test_the_sessions_own_output_is_admitted_without_a_decision(
    self, monkeypatch, capsys, session, kept
  ):
    path = getattr(session, kept)
    assert _decide(monkeypatch, capsys, session.payload(str(path))) is None

  def test_a_link_resolving_into_a_folder_is_admitted(self, monkeypatch, capsys, session):
    link = _link(session.workspace_file.parent / 'output', session.background)
    assert _decide(monkeypatch, capsys, session.payload(str(link))) is None

  def test_a_workspace_file_is_denied_naming_what_the_session_may_read(
    self, monkeypatch, capsys, session
  ):
    decision = _decide(monkeypatch, capsys, session.payload(str(session.workspace_file)))

    assert decision is not None and decision['permissionDecision'] == 'deny'
    reason = decision['permissionDecisionReason']
    assert str(session.background.parent.parent) in reason
    assert str(session.oversized.parent.parent) in reason
    assert f'{session.workspace_file} resolves to {session.workspace_file}, outside both' in reason

  @pytest.mark.parametrize('kept', ['background', 'oversized'])
  def test_a_link_out_of_a_folder_is_denied(self, monkeypatch, capsys, session, kept):
    link = _link(getattr(session, kept).parent / 'escape', session.workspace_file)
    decision = _decide(monkeypatch, capsys, session.payload(str(link)))
    assert decision is not None and decision['permissionDecision'] == 'deny'

  def test_another_sessions_folders_and_the_transcript_are_denied(
    self, monkeypatch, capsys, session
  ):
    other = Path(str(session.oversized).replace(_SESSION, f'{_SESSION}-other'))
    other.parent.mkdir(parents=True)
    other.write_text('content\n')
    climbing = session.oversized.parent.parent / '..' / other.relative_to(session.transcript.parent)
    for path in (other, climbing, session.transcript):
      decision = _decide(monkeypatch, capsys, session.payload(str(path)))
      assert decision is not None and decision['permissionDecision'] == 'deny', path

  def test_a_path_that_does_not_resolve_is_denied(self, monkeypatch, capsys, session):
    missing = session.background.parent / 'missing.output'
    loop = _link(session.background.parent / 'loop', session.background.parent / 'loop')
    for path in (missing, loop):
      decision = _decide(monkeypatch, capsys, session.payload(str(path)))
      assert decision is not None and 'does not resolve' in decision['permissionDecisionReason']

  def test_a_relative_path_is_denied(self, monkeypatch, capsys, session):
    monkeypatch.chdir(session.background.parent)
    decision = _decide(monkeypatch, capsys, session.payload(session.background.name))
    assert (
      decision is not None and 'is not an absolute path' in decision['permissionDecisionReason']
    )


class TestFailClosed:
  @pytest.mark.parametrize(
    'malformed',
    [
      {'session_id': '..'},
      {'session_id': 'a/b'},
      {'session_id': ''},
      {'session_id': None},
      {'transcript_path': 'projects/-workspace/session.jsonl'},
      {'tool_input': {}},
      {'tool_input': {'file_path': 7}},
    ],
  )
  def test_an_unreadable_input_raises(self, monkeypatch, session, malformed):
    payload = {**session.payload(str(session.background)), **malformed}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    with pytest.raises((KeyError, ValueError)):
      read_gate.main(['read_gate'])

  def test_a_session_without_its_temp_root_raises(self, monkeypatch, session):
    monkeypatch.delenv(read_gate.TEMP_ROOT_ENV)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(session.payload('/etc/hostname'))))
    with pytest.raises(KeyError):
      read_gate.main(['read_gate'])

  def test_a_failing_gate_blocks_the_call(self, session):
    # Claude blocks a call whose PreToolUse hook exits 2, showing the model its stderr
    gate = subprocess.run(
      [sys.executable, '-m', 'ride.claude.read_gate'],
      input='not json',
      capture_output=True,
      text=True,
      env={**os.environ, read_gate.TEMP_ROOT_ENV: str(session.temp_root)},
    )
    assert gate.returncode == 2
    assert gate.stderr.startswith('read gate failed, denying the call')


def test_the_settings_command_gates_read_from_the_sessions_working_directory(session):
  (entry,) = gate_hooks(mcp.Reach(), None)['PreToolUse']
  (hook,) = entry['hooks']

  def run(file_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
      hook['command'],
      shell=True,
      cwd=session.workspace_file.parent,
      input=json.dumps(session.payload(str(file_path))),
      capture_output=True,
      text=True,
      env={**os.environ, read_gate.TEMP_ROOT_ENV: str(session.temp_root)},
    )

  admitted, denied = run(session.background), run(session.workspace_file)
  assert (admitted.returncode, admitted.stdout) == (0, ''), admitted.stderr
  assert denied.returncode == 0, denied.stderr
  assert json.loads(denied.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
