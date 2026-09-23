"""Live print-mode probes for the Stop hook input `ride.claude.stop_guard` reads.

Claude Code sends a Stop hook the session's running background tasks as
`background_tasks`, each with its id, status, and command, and fires the hook
in print mode. Undocumented, so the contract is held here against the pinned
Claude Code for both task kinds a session leaves running, a Monitor and a
background shell.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ride.claude.stop_guard as stop_guard
from bro.monitor import SESSION_DIR_ENV
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
  pinned_claude,
)

_MONITOR_COMMAND = 'sleep 20'
_WATCH_COMMAND = 'watch-run sleep 30'
_HOOK_SCRIPT = """
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
with Path(sys.argv[1]).open('a') as dump:
  dump.write(json.dumps(payload) + '\\n')
"""

pytestmark = REQUIRES_CLAUDE_CREDENTIAL


@pytest.fixture(scope='module')
def claude(pytestconfig: pytest.Config) -> Path:
  return pinned_claude(pytestconfig)


def _stops(
  tmp_path: Path, claude: Path, prompt: str, *, tools: str, environment: dict[str, str]
) -> list[dict]:
  """run a haiku print session whose Stop hook dumps its payloads, and return them."""
  hook = tmp_path / 'hook.py'
  hook.write_text(_HOOK_SCRIPT)
  dump = tmp_path / 'stops.jsonl'
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  (config / 'settings.json').write_text(
    json.dumps(
      {
        'skipDangerousModePermissionPrompt': True,
        'hooks': {
          'Stop': [
            {
              'hooks': [
                {'type': 'command', 'command': f'{sys.executable} {hook} {dump}'},
              ]
            }
          ]
        },
      }
    )
  )
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  env['CLAUDE_CODE_OAUTH_TOKEN'] = claude_token() or ''
  env['CLAUDE_CONFIG_DIR'] = str(config)
  env['DISABLE_AUTOUPDATER'] = '1'
  env.update(environment)
  completed = subprocess.run(
    [
      str(claude),
      '-p',
      '--model',
      'haiku',
      '--allowedTools',
      tools,
      '--dangerously-skip-permissions',
      prompt,
    ],
    cwd=tmp_path,
    env=env,
    capture_output=True,
    text=True,
    timeout=180,
    check=False,
  )
  assert completed.returncode == 0, completed.stderr
  stops = [json.loads(line) for line in dump.read_text().splitlines()]
  assert len(stops) >= 1, completed.stdout
  return stops


def _running_task(stop: dict) -> dict:
  assert stop['hook_event_name'] == 'Stop'
  assert stop['stop_hook_active'] is False
  [task] = stop['background_tasks']
  assert task['status'] == 'running'
  assert isinstance(task['id'], str) and len(task['id']) > 0
  return task


def test_print_mode_stops_carry_a_running_monitor(tmp_path: Path, claude: Path) -> None:
  prompt = (
    'Call the Monitor tool exactly once with timeout_ms=5000, description '
    f"'probe', and command exactly: {_MONITOR_COMMAND}\n"
    'Then end your turn immediately, replying with the single word ARMED and nothing else. '
    'If a notification arrives later, reply with the single word DONE and end your turn.'
  )

  stops = _stops(tmp_path, claude, prompt, tools='Monitor', environment={})

  task = _running_task(stops[0])
  assert task['command'] == _MONITOR_COMMAND
  reason = stop_guard.notice(stops[0], [], summoned=False)
  assert reason is not None
  assert task['id'] in reason
  assert _MONITOR_COMMAND in reason


def test_print_mode_stops_carry_a_background_watch_run_as_typed(
  tmp_path: Path, claude: Path
) -> None:
  watch_run = shutil.which('watch-run')
  assert watch_run is not None, 'watch-run is not on PATH'
  session = tmp_path / 'session'
  session.mkdir()
  prompt = (
    'Use the Bash tool with run_in_background set to true to run exactly this command: '
    f'{_WATCH_COMMAND}\n'
    'Then end your turn immediately, replying with the single word ARMED and nothing else.'
  )

  stops = _stops(
    tmp_path,
    claude,
    prompt,
    tools='Bash',
    environment={
      SESSION_DIR_ENV: str(session),
      'PATH': f'{Path(watch_run).parent}:{os.environ["PATH"]}',
    },
  )

  task = _running_task(stops[0])
  assert task['command'] == _WATCH_COMMAND
  reason = stop_guard.notice(stops[0], [], summoned=False)
  assert reason is not None
  assert reason.startswith('A watch runs with no `watch-next` waiting')
  assert task['id'] in reason
