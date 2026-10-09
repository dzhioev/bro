"""conformance: a working session marks its workspace active as it works, not
only when it starts and ends.

`ride list` dates a workspace by its session's activity file. The session
runner touches it at the session's start and end; the harness has to touch it
between, or a session busy for hours reads as idle since its start. The
session's second shell command, run after its first one finished, reads the
file's mtime itself: from inside the session no end mark can have happened yet,
so only a touch the harness made in between dates it past that first command's
end."""

import secrets
import shlex
import sys
from pathlib import Path
from unittest import mock

from bro.local.conformance_test_helper import harness_matrix, run_unattended
from bro.monitor import workspace_session_dir
from bro.workspace.paths import workspace_dir
from ride.workspace.model import ACTIVITY_FILENAME

_PROBE = 'bro-watch-probe'
_PRINT_MTIME = 'import os, sys; print(os.stat(sys.argv[1]).st_mtime_ns)'


def _commands(messages: list[dict]) -> list[str]:
  return [
    str(message['arguments'].get('command', ''))
    for message in messages
    if message['type'] == 'tool_call'
  ]


@harness_matrix()
def test_a_working_session_marks_its_workspace_active_between_its_start_and_end(
  harness: str, recipe: str, tmp_path: Path, conformance_data: Path
) -> None:
  name = f'activity-probe-{secrets.token_hex(4)}'
  with mock.patch.dict('os.environ', {'XDG_DATA_HOME': str(conformance_data)}):
    activity_file = workspace_session_dir(workspace_dir(name)) / ACTIVITY_FILENAME
  tree = tmp_path / 'tree'
  tree.mkdir()
  first_done = tree / 'first-done'
  marked = tree / 'marked'
  first = tree / 'first'
  first.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(first_done))}\n')
  second = tree / 'second'
  read_mark = shlex.join([sys.executable, '-c', _PRINT_MTIME, str(activity_file)])
  second.write_text(f'#!/bin/sh\n{read_mark} > {shlex.quote(str(marked))}\n')
  for script in (first, second):
    script.chmod(0o755)

  messages = run_unattended(
    harness=harness,
    recipe=recipe,
    bro=_PROBE,
    prompt=(
      f'Run `{first}` in the shell. Once it has finished, run `{second}` in the shell '
      'as a separate command. Then answer with done and nothing else.'
    ),
    tree=tree,
    data=conformance_data,
    workspace=name,
  )

  commands = _commands(messages)
  firsts = [index for index, command in enumerate(commands) if str(first) in command]
  seconds = [index for index, command in enumerate(commands) if str(second) in command]
  assert len(firsts) == 1 and len(seconds) == 1 and firsts[0] < seconds[0], commands
  assert int(marked.read_text()) > first_done.stat().st_mtime_ns
