"""Live print-mode probe for the Stop hook input `ride.claude.stop_guard` reads.

Claude Code sends a Stop hook the session's running background tasks as
`background_tasks`, each with its id, status, and command, and fires the hook
in print mode. Undocumented, so the contract is held here against the `claude`
on PATH; the container pins its version in `ride/setup/container/claude-code-version`.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ride.claude.stop_guard as stop_guard
from bro.base import credentials
from bro.base.suite_environment import host_credential_store

_MONITOR_COMMAND = 'sleep 20'
_HOOK_SCRIPT = """
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
with Path(sys.argv[1]).open('a') as dump:
  dump.write(json.dumps(payload) + '\\n')
"""


def _claude_token() -> str | None:
  with host_credential_store():
    return credentials.try_get('claude_code')


pytestmark = pytest.mark.skipif(
  shutil.which('claude') is None or _claude_token() is None,
  reason='needs the claude executable and the claude_code credential',
)


def test_print_mode_stops_carry_the_running_background_tasks(tmp_path: Path) -> None:
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
  env['CLAUDE_CODE_OAUTH_TOKEN'] = _claude_token() or ''
  env['CLAUDE_CONFIG_DIR'] = str(config)
  env['DISABLE_AUTOUPDATER'] = '1'
  prompt = (
    'Call the Monitor tool exactly once with persistent=false, timeout_ms=5000, description '
    f"'probe', and command exactly: {_MONITOR_COMMAND}\n"
    'Then end your turn immediately, replying with the single word ARMED and nothing else. '
    'If a notification arrives later, reply with the single word DONE and end your turn.'
  )
  completed = subprocess.run(
    [
      'claude',
      '-p',
      '--model',
      'haiku',
      '--allowedTools',
      'Monitor',
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
  first = stops[0]
  assert first['hook_event_name'] == 'Stop'
  assert first['stop_hook_active'] is False
  [task] = first['background_tasks']
  assert task['status'] == 'running'
  assert task['command'] == _MONITOR_COMMAND
  assert isinstance(task['id'], str) and len(task['id']) > 0
  reason = stop_guard.notice(first, [], 'full', summoned=False)
  assert reason is not None
  assert task['id'] in reason
  assert _MONITOR_COMMAND in reason
