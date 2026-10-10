"""Claude `PostToolUse` hook stopping the turn of a call that ended the session.

A service tool ends a session (`ride.claude.harness`) by leaving its exit status
and signaling the runner while the call is in flight. Answering that call with
`continue: false` keeps the call's own result, makes Claude take no further
model call, and has it write the stopped turn with a `hook_stopped_continuation`
record naming the call.
"""

import json
import sys
from pathlib import Path

from bro.workspace.session import requested_exit_status
from ride.claude.session_end_state import StoppedCall, StoppedCallMark


def main(argv: list[str]) -> int:
  if len(argv) != 1:
    raise ValueError(f'usage: {argv[0]}')
  payload = json.load(sys.stdin)
  if requested_exit_status() is None:
    return 0
  call_id = payload['tool_use_id']
  if not isinstance(call_id, str) or call_id == '':
    raise ValueError(f'malformed tool use id: {call_id!r}')
  transcript = payload['transcript_path']
  if not isinstance(transcript, str) or not Path(transcript).is_absolute():
    raise ValueError(f'malformed transcript path: {transcript!r}')
  StoppedCallMark.for_session().record(StoppedCall(call_id, Path(transcript)))
  print(json.dumps({'continue': False, 'stopReason': 'the session ended'}))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
