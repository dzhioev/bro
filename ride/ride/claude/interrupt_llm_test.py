"""Live probes of the stdin-open print session `ride.claude.interrupt.run_streaming`
drives, against the pinned Claude Code.

Claude Code starts a turn of its own when a background task finishes only while
the session's stdin is open, and holds the session for as long as it is; a
single-shot print session exits moments after its final reply instead. The
runner reads the running tasks off the `background_tasks_changed` stream event
to settle each turn end, so both are held here.
"""

import json
import os
from pathlib import Path

import ride.claude.interrupt as interrupt
from ride.claude.claude_argv import STREAM_JSON_ARGS
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  bounded,
  claude_token,
  watched_session,
)

_SESSION_TIMEOUT_SECONDS = 300.0

pytestmark = REQUIRES_CLAUDE_CREDENTIAL


def _session_env(tmp_path: Path) -> dict[str, str]:
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  env['CLAUDE_CODE_OAUTH_TOKEN'] = claude_token() or ''
  env['CLAUDE_CONFIG_DIR'] = str(config)
  env['DISABLE_AUTOUPDATER'] = '1'
  return env


def _words(run: interrupt.StreamedRun) -> list[str]:
  return [reply.strip().strip('.').lower() for reply in run.results]


def _user_texts(config: Path) -> list[str]:
  texts = []
  for path in (config / 'projects').rglob('*.jsonl'):
    for line in path.read_text().splitlines():
      record = json.loads(line)
      content = record.get('message', {}).get('content') if record.get('type') == 'user' else None
      if isinstance(content, str):
        texts.append(content)
  return texts


def test_a_running_background_task_draws_a_notice_and_its_end_wakes_the_last_turn(
  tmp_path: Path, claude: Path, monkeypatch
) -> None:
  monkeypatch.chdir(tmp_path)
  prompt = (
    'Use the Bash tool with run_in_background set to true to run exactly this command: '
    'sleep 15 && echo slept-ok. Do not wait for it and do not poll it. Reply with the single '
    'word started and end your turn. When a message tells you the turn ended with work still '
    'live, reply with the single word noted and end your turn. Later, when a notification '
    'tells you that background command finished, reply with the single word finished and end '
    'your turn.'
  )
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--allowedTools',
    'Bash',
    '--dangerously-skip-permissions',
    '--max-turns',
    '6',
    *STREAM_JSON_ARGS,
  ]
  env = _session_env(tmp_path)

  with watched_session(tmp_path / 'session') as (store, waiters):
    with bounded(_SESSION_TIMEOUT_SECONDS):
      run = interrupt.run_streaming(argv, env, prompt, store=store, waiters=waiters)

  assert run.code == 0 and not run.stopped, run
  assert _words(run) == ['started', 'noted', 'finished'], run
  [notice] = [text for text in _user_texts(tmp_path / 'config') if 'background work: ' in text]
  assert 'local_bash' in notice
