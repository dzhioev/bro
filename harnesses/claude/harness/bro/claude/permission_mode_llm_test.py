"""Live probes of the permission modes a session's Claude runs in, against the
pinned Claude Code.

The argv builder passes the mode its hold and isolation select through
`--permission-mode`, and its permission settings through `--settings`.
The probes hold what a session records under each:
a TUI starts in its mode with no dialog in the way of its first prompt,
and an unboxed session's settings keep bypass mode off over each flag that asks for it.
"""

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from bro.claude.claude_argv import permission_mode, permission_settings
from bro.claude.claude_config import _SESSION_SETTINGS_JSON
from bro.claude.live_claude_test_helper import REQUIRES_CLAUDE_CREDENTIAL, claude_token, terminal
from ride.workspace.metadata import Isolation

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300
_AWAIT_SECONDS = 180.0
_POLL_SECONDS = 0.2
_PROMPT = 'Reply with the single word READY.'


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


def _recorded_modes(config: Path) -> list[str]:
  """the permission mode of each prompt the session's transcript records."""
  return [
    record['permissionMode']
    for path in sorted((config / 'projects').rglob('*.jsonl'))
    for record in map(json.loads, path.read_text().splitlines())
    if record.get('type') == 'user' and 'permissionMode' in record
  ]


def _await(condition: Callable[[], bool], what: str) -> None:
  deadline = time.monotonic() + _AWAIT_SECONDS
  while not condition():
    assert time.monotonic() < deadline, f'{what} within {_AWAIT_SECONDS:.0f}s'
    time.sleep(_POLL_SECONDS)


@pytest.mark.parametrize(
  ('hold', 'isolation', 'recorded'),
  [
    # the transcript names manual mode `default`
    ('guided', Isolation.BOXED, 'default'),
    ('guided', Isolation.UNBOXED, 'default'),
    ('attended', Isolation.BOXED, 'bypassPermissions'),
    ('attended', Isolation.UNBOXED, 'auto'),
  ],
)
def test_a_tui_takes_its_first_prompt_in_its_holds_mode(
  tmp_path: Path, claude: Path, hold: str, isolation: Isolation, recorded: str
) -> None:
  config = _config(tmp_path)
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--permission-mode',
    permission_mode(hold, isolation),
    '--settings',
    json.dumps(permission_settings(isolation)),
    _PROMPT,
  ]
  with terminal(argv, _environment(config), tmp_path):
    _await(lambda: len(_recorded_modes(config)) > 0, 'the TUI never took its prompt')

  assert set(_recorded_modes(config)) == {recorded}


@pytest.mark.parametrize(
  'asking', [['--dangerously-skip-permissions'], ['--permission-mode', 'bypassPermissions']]
)
def test_unboxed_settings_keep_bypass_mode_off(
  tmp_path: Path, claude: Path, asking: list[str]
) -> None:
  config = _config(tmp_path)
  subprocess.run(
    [
      str(claude),
      '-p',
      '--model',
      'haiku',
      '--max-turns',
      '1',
      *asking,
      '--settings',
      json.dumps(permission_settings(Isolation.UNBOXED)),
      _PROMPT,
    ],
    cwd=tmp_path,
    env=_environment(config),
    stdin=subprocess.DEVNULL,
    check=True,
    capture_output=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
  )

  assert set(_recorded_modes(config)) == {'default'}
