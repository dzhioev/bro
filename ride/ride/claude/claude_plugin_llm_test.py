"""Live probes of the ride plugin (`ride.claude.claude_plugin`) against the
pinned Claude Code.

The plugin rests on Claude Code's hooks-module API, which the release marks
early access: the plugin is held to Claude Code's own validator and its own
tests there, and a watch rewake is held to reach the transcript line the plugin
draws, read off print mode's stream, where that line arrives as a `ui_log` event.
"""

import json
import os
import subprocess
from pathlib import Path

from ride.claude import claude_plugin
from ride.claude.claude_argv import HOOKS_ON, STREAM_JSON_ARGS, watch_waiter_hooks
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  Feed,
  bounded,
  claude_token,
  watched_session,
)

_SESSION_TIMEOUT_SECONDS = 300.0
_CHECK_TIMEOUT_SECONDS = 120.0
_LINE = 'PROBE-LINE-4711'


def _environment(root: Path, **extra: str) -> dict[str, str]:
  config = root / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  environment = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  environment.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
    **extra,
  )
  return environment


def test_the_plugin_passes_claude_codes_validator_and_its_own_tests(
  tmp_path: Path, claude: Path
) -> None:
  plugin = claude_plugin.provision(tmp_path / 'state')
  environment = _environment(tmp_path)

  for check in ('validate', 'test'):
    completed = subprocess.run(
      [str(claude), 'plugin', check, str(plugin)],
      env=environment,
      capture_output=True,
      text=True,
      timeout=_CHECK_TIMEOUT_SECONDS,
      check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@REQUIRES_CLAUDE_CREDENTIAL
def test_a_rewake_draws_its_batch_through_the_plugin(tmp_path: Path, claude: Path) -> None:
  plugin = claude_plugin.provision(tmp_path / 'state')
  settings = json.dumps({**HOOKS_ON, 'hooks': watch_waiter_hooks()}, separators=(',', ':'))
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--dangerously-skip-permissions',
    '--plugin-dir',
    str(plugin),
    '--settings',
    settings,
    *STREAM_JSON_ARGS,
  ]
  logged: list[str] = []
  with watched_session(tmp_path / 'session') as (store, waiters):
    feed = Feed(store, tmp_path / 'lines')
    environment = _environment(
      tmp_path, **{claude_plugin.REWAKE_RECORD_ENV: str(waiters.rewake_record)}
    )
    prompt = {
      'type': 'user',
      'message': {'role': 'user', 'content': 'Reply with the single word READY.'},
    }
    with (
      bounded(_SESSION_TIMEOUT_SECONDS),
      subprocess.Popen(
        argv,
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
      ) as process,
    ):
      assert process.stdin is not None and process.stdout is not None
      process.stdin.write(json.dumps(prompt) + '\n')
      process.stdin.flush()
      results = 0
      for line in process.stdout:
        event = json.loads(line)
        if event.get('subtype') == 'ui_log' and event.get('plugin') == 'ride':
          logged.append(event['text'])
        elif event.get('type') == 'result':
          results += 1
          if results == 2:
            break
          feed.say(_LINE)
      feed.stop()
      waiters.stand_down()
      process.stdin.close()
    lines = json.loads(waiters.rewake_record.read_text())['lines']

  assert [line['content'] for line in lines if _LINE in line['content']] != []
  assert len(logged) == len(lines)
  assert all(row.endswith(line['content']) for row, line in zip(logged, lines, strict=True))
