"""Live probes of the transcripts the pinned Claude Code writes when a session
is resumed, cleared or branched, of the `SessionStart` hooks it runs then, and
of the trails and the resume notice a resumed session records.

The session recorder decides each trail's edge from those files alone
(`trail_recorder.py`): a resume has to append to the transcript it continues,
`/clear` has to open one sharing no record with it, and `/branch` and
`--fork-session` have to copy its records, uuids kept, into a new one, with
`/branch` naming the transcript each record came from. The session's
`SessionStart` hook (`session_start.py`) registers each transcript it is told,
and on a resume returns the notice as additional context, which has to reach
the request and the transcript ahead of the next user line; a resume that
ends before any input has to leave no line carrying a uuid. The route through
`ride` resumes a recorded session, branches it, and reads the edges its trails
got and the notice its transcript carries."""

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

from bro.base import configs
from bro.base.spawn import console_script
from bro.claude import claude_release
from bro.claude.claude_config import _SESSION_SETTINGS_JSON
from bro.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  bounded,
  claude_token,
  terminal,
)
from bro.claude.provisioning import claude_code_version
from bro.monitor import workspace_session_dir
from bro.trails.local import LocalStore
from bro.trails.record.recording_map import FILENAME
from bro.workspace.launch_facts import Launches, resume_notice
from bro.workspace.paths import ride_trails_dir, workspace_dir

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 600
_AWAIT_SECONDS = 180.0
_POLL_SECONDS = 0.2


def _config(root: Path) -> Path:
  """a Claude config dir for a session in `root` under the session settings."""
  config = root / 'config'
  config.mkdir()
  state = {
    'hasCompletedOnboarding': True,
    'projects': {str(root): {'hasTrustDialogAccepted': True}},
  }
  (config / '.claude.json').write_text(json.dumps(state))
  (config / 'settings.json').write_text(json.dumps(_SESSION_SETTINGS_JSON))
  return config


def _environment(config: Path) -> dict[str, str]:
  environment = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  environment.update(CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '', CLAUDE_CONFIG_DIR=str(config))
  return environment


def _await(condition: Callable[[], bool], what: str) -> None:
  deadline = time.monotonic() + _AWAIT_SECONDS
  while not condition():
    assert time.monotonic() < deadline, f'{what} within {_AWAIT_SECONDS:.0f}s'
    time.sleep(_POLL_SECONDS)


def _transcripts(projects: Path) -> list[Path]:
  """the session transcripts under a Claude projects dir, leaving out subagents'."""
  return sorted(projects.glob('*/*.jsonl'))


def _records(path: Path) -> list[dict]:
  return [json.loads(line) for line in path.read_text().splitlines()]


def _replied(records: list[dict], word: str) -> bool:
  return any(
    record.get('type') == 'assistant'
    and any(
      block.get('type') == 'text' and word in block.get('text', '')
      for block in record['message']['content']
    )
    for record in records
  )


def _print(claude: Path, config: Path, cwd: Path, prompt: str, *arguments: str) -> None:
  subprocess.run(
    [str(claude), '-p', '--model', 'haiku', *arguments, prompt],
    cwd=cwd,
    env=_environment(config),
    stdin=subprocess.DEVNULL,
    check=True,
    capture_output=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
  )


def _first_own(records: list[dict], origin: list[dict]) -> Optional[int]:
  """the first record of a copy whose uuid its origin lacks."""
  carried = {record['uuid'] for record in origin if 'uuid' in record}
  return next(
    (
      line
      for line, record in enumerate(records)
      if 'uuid' in record and record['uuid'] not in carried
    ),
    None,
  )


@pytest.mark.parametrize('mode', ['print', 'tui'])
def test_a_resume_appends_to_the_transcript_it_continues(
  tmp_path: Path, claude: Path, mode: str
) -> None:
  config = _config(tmp_path)
  _print(claude, config, tmp_path, 'Reply with the single word ONE.')
  [transcript] = _transcripts(config / 'projects')
  before = transcript.read_text().splitlines()

  prompt = 'Reply with the single word TWO.'
  if mode == 'print':
    _print(claude, config, tmp_path, prompt, '--resume', transcript.stem)
  else:
    argv = [str(claude), '--model', 'haiku', '--resume', transcript.stem, prompt]
    with terminal(argv, _environment(config), tmp_path):
      _await(lambda: _replied(_records(transcript)[len(before) :], 'TWO'), 'no reply to the resume')

  assert _transcripts(config / 'projects') == [transcript]
  assert transcript.read_text().splitlines()[: len(before)] == before


def test_clear_opens_a_transcript_sharing_no_record(tmp_path: Path, claude: Path) -> None:
  config = _config(tmp_path)
  argv = [str(claude), '--model', 'haiku', 'Reply with the single word ONE.']
  with terminal(argv, _environment(config), tmp_path) as tui:
    _await(lambda: len(_transcripts(config / 'projects')) == 1, 'no transcript')
    [first] = _transcripts(config / 'projects')
    _await(lambda: _replied(_records(first), 'ONE'), 'no reply to the first prompt')
    tui.type_line('/clear')
    _await(lambda: len(_transcripts(config / 'projects')) == 2, 'no transcript after /clear')
    [cleared] = [path for path in _transcripts(config / 'projects') if path != first]
    tui.type_line('Reply with the single word TWO.')
    _await(lambda: _replied(_records(cleared), 'TWO'), 'no reply after /clear')

  first_uuids = {record.get('uuid') for record in _records(first)} - {None}
  cleared_uuids = {record.get('uuid') for record in _records(cleared)} - {None}
  assert len(cleared_uuids) > 0
  assert first_uuids.isdisjoint(cleared_uuids)


def test_branch_copies_the_records_naming_the_transcript_they_came_from(
  tmp_path: Path, claude: Path
) -> None:
  config = _config(tmp_path)
  argv = [str(claude), '--model', 'haiku', 'Reply with the single word ONE.']
  with terminal(argv, _environment(config), tmp_path) as tui:
    _await(lambda: len(_transcripts(config / 'projects')) == 1, 'no transcript')
    [first] = _transcripts(config / 'projects')
    _await(lambda: _replied(_records(first), 'ONE'), 'no reply to the first prompt')
    tui.type_line('/branch')
    _await(lambda: len(_transcripts(config / 'projects')) == 2, 'no transcript after /branch')
    [branch] = [path for path in _transcripts(config / 'projects') if path != first]
    _await(
      lambda: _first_own(_records(branch), _records(first)) is not None,
      'no record of its own in the branch',
    )

  origin = _records(first)
  records = _records(branch)
  own = _first_own(records, origin)
  assert own is not None
  copied = [record for record in records[:own] if 'uuid' in record]
  assert [record['uuid'] for record in copied] == [
    record['uuid'] for record in origin if 'uuid' in record
  ][: len(copied)]
  assert len(copied) > 0
  assert all(
    record['forkedFrom'] == {'sessionId': first.stem, 'messageUuid': record['uuid']}
    and record['sessionId'] == branch.stem
    for record in copied
  )


def test_a_forked_session_copies_the_records_without_naming_their_transcript(
  tmp_path: Path, claude: Path
) -> None:
  config = _config(tmp_path)
  _print(claude, config, tmp_path, 'Reply with the single word ONE.')
  [first] = _transcripts(config / 'projects')
  before = first.read_text()

  _print(
    claude,
    config,
    tmp_path,
    'Reply with the single word TWO.',
    '--resume',
    first.stem,
    '--fork-session',
  )

  assert first.read_text() == before
  [fork] = [path for path in _transcripts(config / 'projects') if path != first]
  origin = _records(first)
  records = _records(fork)
  own = _first_own(records, origin)
  assert own is not None and _replied(records[own:], 'TWO')
  copy_start = next(line for line, record in enumerate(records) if 'uuid' in record)
  assert copy_start > 0
  copied = records[copy_start:own]
  assert [record['uuid'] for record in copied] == [
    record['uuid'] for record in origin if 'uuid' in record
  ]
  assert all('forkedFrom' not in record and record['sessionId'] == fork.stem for record in copied)


_SECRET = 'PELICAN'


def _hooked_settings(root: Path) -> tuple[str, Path]:
  """`--settings` running a `SessionStart` hook that logs what it is told, and on a
  resume adds a context naming the secret word; with the log it writes."""
  starts = root / 'starts.jsonl'
  hook = root / 'hook.py'
  hook.write_text(
    'import json, sys\n'
    'payload = json.load(sys.stdin)\n'
    f'with open({str(starts)!r}, "a") as starts:\n'
    '  starts.write(json.dumps(payload) + "\\n")\n'
    'if payload["source"] == "resume":\n'
    '  context = "The secret word is ' + _SECRET + '."\n'
    '  output = {"hookEventName": "SessionStart", "additionalContext": context}\n'
    '  print(json.dumps({"hookSpecificOutput": output}))\n'
  )
  command = shlex.join([sys.executable, str(hook)])
  settings = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': command}]}]}}
  return json.dumps(settings), starts


def _starts(log: Path) -> list[dict]:
  return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize('mode', ['print', 'tui'])
def test_a_resume_hooks_context_reaches_the_request_ahead_of_the_next_user_line(
  tmp_path: Path, claude: Path, mode: str
) -> None:
  config = _config(tmp_path)
  settings, starts = _hooked_settings(tmp_path)
  _print(claude, config, tmp_path, 'Reply with the single word ONE.', '--settings', settings)
  [transcript] = _transcripts(config / 'projects')
  before = len(_records(transcript))

  prompt = 'Reply with the secret word alone.'
  if mode == 'print':
    _print(claude, config, tmp_path, prompt, '--settings', settings, '--resume', transcript.stem)
  else:
    argv = [str(claude), '--model', 'haiku', '--settings', settings, '--resume', transcript.stem]
    with terminal(argv, _environment(config), tmp_path) as tui:
      _await(lambda: len(_starts(starts)) == 2, 'no SessionStart for the resume')
      tui.type_line(prompt)
      _await(lambda: _replied(_records(transcript)[before:], _SECRET), 'no reply naming the word')

  assert [(start['source'], start['transcript_path']) for start in _starts(starts)] == [
    ('startup', str(transcript)),
    ('resume', str(transcript)),
  ]
  records = _records(transcript)[before:]
  [context] = [
    line
    for line, record in enumerate(records)
    if record.get('type') == 'attachment'
    and record['attachment']['type'] == 'hook_additional_context'
  ]
  assert records[context]['attachment']['content'] == [f'The secret word is {_SECRET}.']
  user = next(line for line, record in enumerate(records) if record.get('type') == 'user')
  assert context < user
  assert records[user]['message']['content'] == prompt
  assert _replied(records[user:], _SECRET)


def test_a_resume_ended_before_any_input_adds_no_line_carrying_a_uuid(
  tmp_path: Path, claude: Path
) -> None:
  config = _config(tmp_path)
  settings, starts = _hooked_settings(tmp_path)
  _print(claude, config, tmp_path, 'Reply with the single word ONE.', '--settings', settings)
  [transcript] = _transcripts(config / 'projects')
  before = len(_records(transcript))
  argv = [str(claude), '--model', 'haiku', '--settings', settings, '--resume', transcript.stem]
  with terminal(argv, _environment(config), tmp_path) as tui:
    _await(lambda: len(_starts(starts)) == 2, 'no SessionStart for the resume')
    tui.type_line('/exit')
    assert tui.process.wait(timeout=_AWAIT_SECONDS) == 0

  added = _records(transcript)[before:]
  assert not any('uuid' in record for record in added)


def test_branch_and_clear_start_the_transcript_each_opens(tmp_path: Path, claude: Path) -> None:
  config = _config(tmp_path)
  settings, starts = _hooked_settings(tmp_path)
  argv = [str(claude), '--model', 'haiku', '--settings', settings, 'Reply with the word ONE.']
  with terminal(argv, _environment(config), tmp_path) as tui:
    _await(lambda: len(_transcripts(config / 'projects')) == 1, 'no transcript')
    [first] = _transcripts(config / 'projects')
    _await(lambda: _replied(_records(first), 'ONE'), 'no reply to the first prompt')
    tui.type_line('/branch')
    _await(lambda: len(_starts(starts)) == 2, 'no SessionStart for the branch')
    tui.type_line('/clear')
    _await(lambda: len(_starts(starts)) == 3, 'no SessionStart for the clear')

  startup, branched, cleared = _starts(starts)
  assert (startup['source'], startup['transcript_path']) == ('startup', str(first))
  assert [branched['source'], cleared['source']] == ['fork', 'clear']
  opened = {Path(branched['transcript_path']), Path(cleared['transcript_path'])}
  assert len(opened) == 2 and first not in opened
  assert all(path.parent == first.parent for path in opened)


def _ride(data: Path, *arguments: str, **options: Any) -> subprocess.CompletedProcess:
  return subprocess.run(
    [console_script('ride'), *arguments],
    env=_ride_environment(data),
    capture_output=True,
    text=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
    **options,
  )


def _ride_environment(data: Path) -> dict[str, str]:
  return {**os.environ, 'XDG_DATA_HOME': str(data), 'BRO_STORE': str(configs.STORE_DIR)}


def _entries(session: Path) -> dict[str, list[dict]]:
  """the recording map's entries by transcript id, empty before it is written."""
  path = session / FILENAME
  if not path.exists():
    return {}
  return {
    record_id: record['trails']
    for record_id, record in json.loads(path.read_text())['records'].items()
  }


def test_a_resumed_and_branched_session_records_resume_and_branch_trails(
  tmp_path: Path, claude: Path
) -> None:
  data = tmp_path / 'data'
  data.mkdir()
  with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}):
    claude_release.seed_binary(claude, claude_code_version(), claude_release.host_platform())
    session = workspace_session_dir(workspace_dir('resume-probe'))
    projects = workspace_dir('resume-probe') / 'claude' / 'projects'
    trails = ride_trails_dir()
  tree = tmp_path / 'tree'
  tree.mkdir()
  started = _ride(
    data,
    'solo',
    '--workspace',
    'resume-probe',
    '--unboxed',
    '--tree',
    str(tree),
    '--harness',
    'claude',
    '--llm',
    'anthropic:claude-haiku-5-5',
    '--hold',
    'unattended',
    '--revoke',
    'trails',
    'bro',
    'Reply with the single word ONE.',
  )
  assert started.returncode == 0, started.stderr
  [transcript] = _transcripts(projects)
  [root] = _entries(session)[transcript.stem]
  assert root['forked_from'] is None
  first_lines = len(_records(transcript))

  argv = [console_script('ride'), 'resume', 'resume-probe']
  with bounded(_SESSION_TIMEOUT_SECONDS), terminal(argv, _ride_environment(data), tree) as tui:
    tui.await_drawn(f'Claude Code v{claude_code_version()}', timeout=_AWAIT_SECONDS)
    tui.type_line('Reply with the single word TWO.')
    _await(lambda: _replied(_records(transcript), 'TWO'), 'no reply to the resumed session')
    _await(lambda: len(_entries(session)[transcript.stem]) == 2, 'no trail for the resume')
    tui.type_line('/branch')
    _await(lambda: len(_transcripts(projects)) == 2, 'no transcript after /branch')
    [branch] = [path for path in _transcripts(projects) if path != transcript]
    _await(lambda: branch.stem in _entries(session), 'no trail for the branch')
    tui.type_line('/exit')
    assert tui.process.wait() == 0

  launches = Launches.load(session).entries
  assert [launch.record for launch in launches] == [transcript.stem, transcript.stem, branch.stem]
  assert [launch.start for launch in launches[:2]] == [0, first_lines]
  continued = _records(transcript)[first_lines:]
  user = next(line for line, record in enumerate(continued) if record.get('type') == 'user')
  assert continued[user]['message']['content'] == 'Reply with the single word TWO.'
  [context] = [
    record['attachment']['content']
    for record in continued[:user]
    if record.get('type') == 'attachment'
    and record['attachment']['type'] == 'hook_additional_context'
  ]
  assert context == [resume_notice(launches[1].facts, launches[0].facts)]

  entries = _entries(session)
  earlier, resumed = entries[transcript.stem]
  [branched] = entries[branch.stem]
  origin = _records(transcript)
  copy = _records(branch)
  own = _first_own(copy, origin)
  assert own is not None
  last_copied = max(line for line in range(own) if 'uuid' in copy[line])
  [original] = [
    line for line, record in enumerate(origin) if record.get('uuid') == copy[last_copied]['uuid']
  ]
  with LocalStore(trails) as store:
    earlier_extent = store.get_trail(earlier['id'])['extent']
    assert earlier['spans'] == [[0, earlier_extent]]
    assert store.get_trail(resumed['id'])['forked_from'] == {
      'trail_id': earlier['id'],
      'step_id': earlier_extent - 1,
      'kind': 'resume',
    }
    assert store.get_trail(branched['id'])['forked_from'] == {
      'trail_id': resumed['id'],
      'step_id': original - resumed['spans'][0][0],
      'kind': 'branch',
    }
  assert branched['spans'] == [[last_copied + 1, None]]
