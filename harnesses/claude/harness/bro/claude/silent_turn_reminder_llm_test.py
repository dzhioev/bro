"""Live probe that the session settings set the text of the reminder Claude Code
adds to the model's input after a run of turns that told the user nothing.

The pinned release reads that text from `CLAUDE_CODE_SILENT_TURN_REMINDER_TEXT`,
which Claude Code documents nowhere. The probe forces the reminder after every
silent turn and holds each one the transcript records to the session settings'
text, so a release that renames the variable or drops the reminder fails here.
"""

import json
import os
import subprocess
from pathlib import Path

from bro.claude.claude_config import _SESSION_SETTINGS_JSON
from bro.claude.live_claude_test_helper import REQUIRES_CLAUDE_CREDENTIAL, claude_token

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300
_REMINDER_ENV_PREFIX = 'CLAUDE_CODE_SILENT_TURN_REMINDER'
_PROMPT = (
  'Run these Bash commands one at a time, each in its own tool call, writing no text between'
  ' them: `echo one`, `echo two`, `echo three`. Then reply DONE.'
)


def _recorded_reminders(claude: Path, tmp_path: Path) -> list[str]:
  """the texts of the silent-turn reminders a print session under the session
  settings records in its transcript."""
  workspace = tmp_path / 'workspace'
  workspace.mkdir()
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(
    json.dumps(
      {
        'hasCompletedOnboarding': True,
        'projects': {str(workspace): {'hasTrustDialogAccepted': True}},
      }
    )
  )
  (config / 'settings.json').write_text(json.dumps(_SESSION_SETTINGS_JSON))
  # the launch environment outranks the settings' own `env`, and a managed
  # session's commands inherit the text its own settings set
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
    and not name.startswith(_REMINDER_ENV_PREFIX)
  }
  env.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    # on for every model, after a single silent turn
    CLAUDE_CODE_SILENT_TURN_REMINDER='1',
    CLAUDE_CODE_SILENT_TURN_REMINDER_TURNS='1',
  )
  subprocess.run(
    [
      str(claude),
      '-p',
      '--model',
      'haiku',
      '--allowedTools',
      'Bash',
      '--dangerously-skip-permissions',
      '--max-turns',
      '8',
      _PROMPT,
    ],
    cwd=workspace,
    env=env,
    stdin=subprocess.DEVNULL,
    check=True,
    capture_output=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
  )

  [transcript] = list((config / 'projects').rglob('*.jsonl'))
  records = [json.loads(line) for line in transcript.read_text().splitlines()]
  return [
    record['attachment']['text']
    for record in records
    if record.get('type') == 'attachment'
    and record['attachment'].get('type') == 'silent_turn_reminder'
  ]


def test_every_silent_turn_reminder_carries_the_session_settings_text(
  claude: Path, tmp_path: Path
) -> None:
  reminders = _recorded_reminders(claude, tmp_path)

  text = _SESSION_SETTINGS_JSON['env']['CLAUDE_CODE_SILENT_TURN_REMINDER_TEXT']
  assert len(reminders) > 0
  assert set(reminders) == {text}
