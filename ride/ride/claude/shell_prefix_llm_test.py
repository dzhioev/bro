"""Live probe of the Bash tool's PATH when Claude Code's shell snapshot times out,
held against the pinned release: without the prefix the session's commands
vanish, with it they resolve.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
  pinned_claude,
)
from ride.claude.shell_prefix import apply_shell_prefix

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

# claude builds the snapshot by sourcing the shell's rc file within 10 seconds
_SLOW_RC = 'sleep 12\n'
_MISSING = 'NO-WATCH-RUN'


@pytest.fixture(scope='module')
def claude(pytestconfig: pytest.Config) -> Path:
  return pinned_claude(pytestconfig)


def _watch_run() -> str:
  watch_run = shutil.which('watch-run')
  assert watch_run is not None, 'watch-run is not on PATH'
  return watch_run


def _session_env(tmp_path: Path) -> dict[str, str]:
  watch_run = _watch_run()
  bash = shutil.which('bash')
  assert bash is not None, 'bash is not on PATH'
  home = tmp_path / 'home'
  home.mkdir()
  (home / '.bashrc').write_text(_SLOW_RC)
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  env.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
    HOME=str(home),
    SHELL=bash,
    PATH=f'{Path(watch_run).parent}:{os.environ["PATH"]}',
  )
  return env


def _bash_output(claude: Path, tmp_path: Path, env: dict[str, str]) -> str:
  completed = subprocess.run(
    [
      str(claude),
      '-p',
      '--model',
      'haiku',
      '--allowedTools',
      'Bash',
      '--dangerously-skip-permissions',
      f'Use the Bash tool to run exactly: command -v watch-run || echo {_MISSING}\n'
      'Then reply with its output verbatim and nothing else.',
    ],
    cwd=tmp_path,
    env=env,
    capture_output=True,
    text=True,
    timeout=240,
    check=False,
  )
  assert completed.returncode == 0, completed.stderr
  return completed.stdout


def test_a_timed_out_snapshot_drops_the_session_path(claude: Path, tmp_path: Path) -> None:
  assert _MISSING in _bash_output(claude, tmp_path, _session_env(tmp_path))


def test_the_prefix_keeps_the_session_path_through_a_timed_out_snapshot(
  claude: Path, tmp_path: Path
) -> None:
  env = _session_env(tmp_path)
  apply_shell_prefix(env, tmp_path)

  output = _bash_output(claude, tmp_path, env)

  assert _MISSING not in output
  assert _watch_run() in output
