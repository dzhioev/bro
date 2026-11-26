import dataclasses
import io
import json
from pathlib import Path
from typing import Optional

import pytest

from bro.claude import session_start
from bro.monitor import SESSION_DIR_ENV
from bro.workspace.launch_facts import Launch, Launches, LaunchFacts, resume_notice
from bro.workspace.launch_facts_test_helper import FACTS

_EARLIER = FACTS
_UPDATED = dataclasses.replace(FACTS, runtime='5d0c41aa90e2')


class _Session:
  """One Claude session's state dir and transcripts, started by lifetimes in turn."""

  def __init__(self, root: Path, monkeypatch, capsys) -> None:
    self.directory = root / 'session'
    self.directory.mkdir()
    self.projects = root / 'projects'
    self.projects.mkdir()
    self._monkeypatch = monkeypatch
    self._capsys = capsys
    monkeypatch.setenv(SESSION_DIR_ENV, str(self.directory))

  def transcript(self, name: str) -> Path:
    return self.projects / f'{name}.jsonl'

  def lifetime(self, facts: LaunchFacts) -> '_Lifetime':
    return _Lifetime(self, facts, len(Launches.load(self.directory).entries))

  def start(self, lifetime: '_Lifetime', transcript: Path, source: str) -> Optional[str]:
    payload = {
      'session_id': transcript.stem,
      'transcript_path': str(transcript),
      'cwd': '/tree',
      'hook_event_name': 'SessionStart',
      'source': source,
    }
    self._monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    argv = ['session_start', str(lifetime.earlier), json.dumps(lifetime.facts.to_wire())]
    assert session_start.main(argv) == 0
    output = self._capsys.readouterr().out
    if output == '':
      return None
    # the hook output Claude Code reads its additional context from
    hook = json.loads(output)['hookSpecificOutput']
    assert hook['hookEventName'] == 'SessionStart'
    return hook['additionalContext']


@dataclasses.dataclass(frozen=True)
class _Lifetime:
  session: _Session
  facts: LaunchFacts
  earlier: int


def _write(transcript: Path, *records: dict) -> None:
  with transcript.open('a') as lines:
    for record in records:
      lines.write(json.dumps(record) + '\n')


def _history(transcript: Path, *names: str) -> None:
  _write(transcript, *({'type': 'user', 'uuid': name} for name in names))


def _bookkeeping(transcript: Path) -> None:
  _write(transcript, {'type': 'cost-state'}, {'type': 'mode'}, {'type': 'permission-mode'})


@pytest.fixture
def session(tmp_path, monkeypatch, capsys) -> _Session:
  return _Session(tmp_path, monkeypatch, capsys)


def test_a_fresh_start_registers_the_transcript_and_gives_no_notice(session):
  transcript = session.transcript('t1')
  assert session.start(session.lifetime(_EARLIER), transcript, 'startup') is None
  _history(transcript, 'a', 'b')
  assert Launches.load(session.directory).entries == (Launch('t1', 0, _EARLIER),)


@pytest.mark.parametrize('ended', ['failed-before-claude', 'closed-before-input', 'killed'])
@pytest.mark.parametrize('runtime', ['earlier', 'updated'])
def test_the_baseline_is_the_newest_lifetime_that_left_history_in_the_transcript(
  session, ended, runtime
):
  transcript = session.transcript('t1')
  session.start(session.lifetime(_EARLIER), transcript, 'startup')
  _history(transcript, 'a', 'b')
  _bookkeeping(transcript)
  if ended != 'failed-before-claude':
    second = session.lifetime(_UPDATED)
    assert session.start(second, transcript, 'resume') is not None
    if ended == 'killed':
      _history(transcript, 'notice', 'load a spell', 'the spell')
    _bookkeeping(transcript)
  facts = _EARLIER if runtime == 'earlier' else _UPDATED
  notice = session.start(session.lifetime(facts), transcript, 'resume')
  baseline = _UPDATED if ended == 'killed' else _EARLIER
  expected = resume_notice(facts, baseline)
  assert notice == expected
  assert ('- the runtime was updated' in expected.splitlines()) == (facts != baseline)


def test_a_resume_registers_at_the_transcripts_line_count(session):
  transcript = session.transcript('t1')
  session.start(session.lifetime(_EARLIER), transcript, 'startup')
  _history(transcript, 'a', 'b')
  _bookkeeping(transcript)
  with transcript.open('a') as lines:
    lines.write('{"type": "user", "uuid": "partial"')
  session.start(session.lifetime(_UPDATED), transcript, 'resume')
  assert Launches.load(session.directory).entries[-1] == Launch('t1', 5, _UPDATED)


def test_a_transcript_turned_to_within_one_lifetime_gives_no_notice(session):
  lifetime = session.lifetime(_EARLIER)
  first, second = session.transcript('t1'), session.transcript('t2')
  session.start(lifetime, first, 'startup')
  _history(first, 'a', 'b')
  assert session.start(lifetime, second, 'clear') is None
  _history(second, 'c')
  assert session.start(lifetime, first, 'resume') is None
  later = session.lifetime(_UPDATED)
  assert session.start(later, second, 'resume') == resume_notice(_UPDATED, _EARLIER)
  assert session.start(later, first, 'resume') == resume_notice(_UPDATED, _EARLIER)


def test_a_transcript_no_lifetime_registered_has_no_baseline(session):
  transcript = session.transcript('t1')
  _history(transcript, 'a', 'b')
  notice = session.start(session.lifetime(_EARLIER), transcript, 'resume')
  assert notice == resume_notice(_EARLIER, None)


@pytest.mark.parametrize('source', ['fork', 'clear', 'compact'])
def test_a_start_other_than_a_resume_registers_without_a_notice(session, source):
  transcript = session.transcript('t1')
  _history(transcript, 'a', 'b')
  assert session.start(session.lifetime(_UPDATED), transcript, source) is None
  assert Launches.load(session.directory).entries == (Launch('t1', 2, _UPDATED),)
