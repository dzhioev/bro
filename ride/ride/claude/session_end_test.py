import io
import json
import os
import subprocess
from pathlib import Path

import pytest

import ride.claude.session_end as session_end
from bro.monitor import SESSION_DIR_ENV
from bro.workspace import session as workspace_session
from ride.claude.claude_argv import session_end_hooks
from ride.claude.session_end_state import ANSWER_TOOL, StoppedCall, StoppedCallMark


@pytest.fixture
def session(tmp_path, monkeypatch) -> Path:
  directory = tmp_path / 'session'
  directory.mkdir()
  monkeypatch.setenv(SESSION_DIR_ENV, str(directory))
  return directory


def _payload(transcript: Path) -> dict:
  return {
    'session_id': 's1',
    'transcript_path': str(transcript),
    'hook_event_name': 'PostToolUse',
    'tool_name': ANSWER_TOOL,
    'tool_input': {'answer': 'the verdict'},
    'tool_use_id': 'toolu_1',
  }


def _end(session: Path) -> None:
  (session / workspace_session.FILENAME).write_text('0')


def _run(monkeypatch, capsys, payload: dict) -> str:
  monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
  assert session_end.main(['session_end']) == 0
  return capsys.readouterr().out


def test_a_call_that_ended_the_session_stops_its_turn_naming_the_call(
  session, tmp_path, monkeypatch, capsys
):
  _end(session)
  transcript = tmp_path / 'transcript.jsonl'

  out = _run(monkeypatch, capsys, _payload(transcript))

  assert json.loads(out)['continue'] is False
  assert StoppedCallMark.for_session().read() == StoppedCall('toolu_1', transcript)


def test_a_call_that_did_not_end_the_session_leaves_its_turn_running(
  session, tmp_path, monkeypatch, capsys
):
  out = _run(monkeypatch, capsys, _payload(tmp_path / 'transcript.jsonl'))

  assert out == ''
  assert StoppedCallMark.for_session().read() is None


@pytest.mark.parametrize(
  'malformed',
  [{'tool_use_id': ''}, {'tool_use_id': None}, {'transcript_path': 'projects/transcript.jsonl'}],
)
def test_an_unreadable_input_raises(session, tmp_path, monkeypatch, malformed):
  _end(session)
  payload = {**_payload(tmp_path / 'transcript.jsonl'), **malformed}
  monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
  with pytest.raises(ValueError):
    session_end.main(['session_end'])


def test_the_settings_command_stops_the_turn(session, tmp_path):
  _end(session)
  entry, _ = session_end_hooks()['PostToolUse']
  (hook,) = entry['hooks']

  stop = subprocess.run(
    hook['command'],
    shell=True,
    input=json.dumps(_payload(tmp_path / 'transcript.jsonl')),
    capture_output=True,
    text=True,
    env=dict(os.environ),
  )

  assert stop.returncode == 0, stop.stderr
  assert json.loads(stop.stdout)['continue'] is False
