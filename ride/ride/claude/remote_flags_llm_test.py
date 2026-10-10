"""Live probe that a session's settings leave Claude Code's remotely served flags
without effect.

The pinned release reads its feature flags from Anthropic's server and, before a
fetch lands, from the copy cached in `.claude.json`; the session settings'
telemetry opt-out keeps both off. The probe seeds one cached flag whose effect
shows in the `init` event — `tengu_birch_kettle`, which lists the
`claude-code-docs` command — and holds that the session settings leave it without
effect, while the same seed with telemetry on lists the command, so a release
that renames the flag or drops the command fails here rather than leaving the
probe nothing to witness. Both sessions run offline, with no credential.
"""

import copy
import json
import os
import subprocess
from pathlib import Path

from ride.claude.claude_argv import STREAM_JSON_ARGS
from ride.claude.claude_config import _SESSION_SETTINGS_JSON

_SESSION_TIMEOUT_SECONDS = 300
_FLAG = 'tengu_birch_kettle'
_COMMAND = 'claude-code-docs'


def _slash_commands(claude: Path, tmp_path: Path, settings: dict) -> list[str]:
  """the slash commands the `init` event lists for a session under `settings`
  whose cached flags hold the probe's flag on."""
  workspace = tmp_path / 'workspace'
  workspace.mkdir()
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(
    json.dumps(
      {
        'hasCompletedOnboarding': True,
        'projects': {str(workspace): {'hasTrustDialogAccepted': True}},
        'cachedGrowthBookFeatures': {_FLAG: True},
      }
    )
  )
  (config / 'settings.json').write_text(json.dumps(settings))
  # the launch environment outranks the settings' own `env`
  env = {
    name: value
    for name, value in os.environ.items()
    if name
    not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY', 'DISABLE_TELEMETRY')
  }
  env.update(CLAUDE_CONFIG_DIR=str(config), DISABLE_AUTOUPDATER='1')
  message = {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}
  completed = subprocess.run(
    [str(claude), *STREAM_JSON_ARGS],
    cwd=workspace,
    env=env,
    input=json.dumps(message) + '\n',
    capture_output=True,
    text=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
  )
  for line in completed.stdout.splitlines():
    event = json.loads(line)
    if event.get('type') == 'system' and event.get('subtype') == 'init':
      return event['slash_commands']
  raise AssertionError(f'no init event: {completed.stdout}\n{completed.stderr}')


def test_the_session_settings_leave_a_cached_flag_without_effect(
  claude: Path, tmp_path: Path
) -> None:
  assert _COMMAND not in _slash_commands(claude, tmp_path, _SESSION_SETTINGS_JSON)


def test_the_cached_flag_takes_effect_with_telemetry_on(claude: Path, tmp_path: Path) -> None:
  settings = copy.deepcopy(_SESSION_SETTINGS_JSON)
  del settings['env']['DISABLE_TELEMETRY']
  assert _COMMAND in _slash_commands(claude, tmp_path, settings)
