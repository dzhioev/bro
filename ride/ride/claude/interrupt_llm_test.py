"""Live probes of the stdin-open print session `ride.claude.interrupt.run_streaming`
drives, against the pinned Claude Code.

Claude Code starts a turn of its own when a background task finishes only while
the session's stdin is open, and holds the session for as long as it is; a
single-shot print session exits moments after its final reply instead. That
behavior is what lets a session wait on a watch at no cost, so it is held here,
along with the session's `watch-next` wake bringing its lines without a file read.
"""

import contextlib
import json
import os
import signal
from collections.abc import Generator
from pathlib import Path

import pytest

import ride.claude.interrupt as interrupt
from bro.monitor import SESSION_DIR_ENV
from ride.claude.claude_argv import STREAM_JSON_ARGS, watch_delivery_hooks
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
  pinned_claude,
)

_SESSION_TIMEOUT_SECONDS = 300.0

pytestmark = REQUIRES_CLAUDE_CREDENTIAL


@pytest.fixture(scope='module')
def claude(pytestconfig: pytest.Config) -> Path:
  return pinned_claude(pytestconfig)


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
  env[SESSION_DIR_ENV] = str(tmp_path / 'session')
  (tmp_path / 'session').mkdir()
  return env


@contextlib.contextmanager
def _bounded(seconds: float) -> Generator[None]:
  """fail the block after `seconds`, so a session that never ends fails the
  probe instead of hanging the stage; the run's own teardown terminates claude."""

  def _expire(signum, frame):
    del signum, frame
    raise TimeoutError(f'the session did not end within {seconds:.0f}s')

  previous = signal.signal(signal.SIGALRM, _expire)
  signal.alarm(int(seconds))
  try:
    yield
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def _run(
  tmp_path: Path, claude: Path, prompt: str, *, tools: str, extra_args: tuple[str, ...] = ()
) -> interrupt.StreamedRun:
  """run a haiku session over stream-json to its own end."""
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--allowedTools',
    tools,
    '--dangerously-skip-permissions',
    '--max-turns',
    '6',
    *extra_args,
    *STREAM_JSON_ARGS,
  ]
  with _bounded(_SESSION_TIMEOUT_SECONDS):
    return interrupt.run_streaming(argv, _session_env(tmp_path), prompt)


def _words(run: interrupt.StreamedRun) -> list[str]:
  return [reply.strip().strip('.').lower() for reply in run.results]


def test_a_finished_background_task_wakes_the_model_and_the_idle_turn_ends_the_session(
  tmp_path: Path, claude: Path, monkeypatch
) -> None:
  monkeypatch.chdir(tmp_path)
  prompt = (
    'Use the Bash tool with run_in_background set to true to run exactly this command: '
    'sleep 15 && echo slept-ok. Do not wait for it and do not poll it. Reply with the single '
    'word started and end your turn. Later, when a notification tells you that background '
    'command finished, reply with the single word finished and end your turn.'
  )

  run = _run(tmp_path, claude, prompt, tools='Bash')

  assert run.code == 0 and not run.stopped, run
  assert _words(run) == ['started', 'finished'], run


def test_a_watch_next_wake_carries_the_watched_line_to_the_model(
  tmp_path: Path, claude: Path, monkeypatch
) -> None:
  monkeypatch.chdir(tmp_path)
  prompt = (
    'Use the Bash tool with run_in_background set to true to run exactly this command: '
    "watch-run bash -c 'sleep 5; echo hello-from-watch; sleep 120'. Then use the Bash tool "
    'with run_in_background set to true to run exactly this command: watch-next. Do not wait '
    'for either and do not poll. Reply with the single word armed and end your turn. Later, '
    'when a notification tells you the watch-next command finished, reply with the line it '
    'printed, quoted exactly, without reading any file; then stop the watch-run command with '
    'the TaskStop tool and end your turn.'
  )

  run = _run(
    tmp_path,
    claude,
    prompt,
    tools='Bash,TaskStop',
    extra_args=(
      '--disallowedTools',
      'Read',
      '--settings',
      json.dumps({'hooks': watch_delivery_hooks()}),
    ),
  )

  assert run.code == 0 and not run.stopped, run
  words = _words(run)
  assert words[0] == 'armed', run
  assert any('hello-from-watch' in word for word in words[1:]), run
