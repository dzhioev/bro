"""Claude `PreToolUse` gate holding `Read` to the calling session's own Claude folders.

Claude Code hands a session some of its own output as files: a backgrounded
command's output under `claude-<uid>/<project>/<session id>/` in its temp root,
`CLAUDE_CODE_TMPDIR`, and the whole of a result too large to inline under the
`<session id>/` folder beside the transcript, whose folder is the `<project>`.
The gate names both from the hook's input and admits a path only when it is
absolute and resolves, symlinks followed, inside one of them; any other path is
denied, as is every call the gate fails on.

One process per call, so it stays stdlib-only and imports nothing of the framework.
"""

import json
import os
import sys
from pathlib import Path

TEMP_ROOT_ENV = 'CLAUDE_CODE_TMPDIR'


def _deny(reason: str) -> None:
  print(
    json.dumps(
      {
        'hookSpecificOutput': {
          'hookEventName': 'PreToolUse',
          'permissionDecision': 'deny',
          'permissionDecisionReason': reason,
        }
      }
    )
  )


def session_folders(payload: dict) -> tuple[Path, Path]:
  """the calling session's own Claude folders: where its backgrounded commands
  write their output, and where its results too large to inline are kept."""
  session = payload['session_id']
  if not isinstance(session, str) or session in ('', '.', '..') or '/' in session:
    raise ValueError(f'malformed session id: {session!r}')
  transcript = payload['transcript_path']
  if not isinstance(transcript, str) or not Path(transcript).is_absolute():
    raise ValueError(f'malformed transcript path: {transcript!r}')
  project = Path(transcript).parent
  return (
    Path(os.environ[TEMP_ROOT_ENV]) / f'claude-{os.getuid()}' / project.name / session,
    project / session,
  )


def main(argv: list[str]) -> int:
  if len(argv) != 1:
    raise ValueError(f'usage: {argv[0]}')
  payload = json.load(sys.stdin)
  folders = session_folders(payload)
  file_path = payload['tool_input']['file_path']
  if not isinstance(file_path, str):
    raise ValueError(f'malformed file path: {file_path!r}')
  rule = (
    'this session has no file tools: Read opens only what Claude Code wrote for it, a '
    f"backgrounded command's output under {folders[0]} and a result too large to inline "
    f'under {folders[1]}'
  )
  path = Path(file_path)
  if not path.is_absolute():
    _deny(f'{rule}; {file_path} is not an absolute path.')
    return 0
  try:
    resolved = path.resolve(strict=True)
  except (OSError, RuntimeError) as error:
    _deny(f'{rule}; {file_path} does not resolve: {error}.')
    return 0
  if any(resolved.is_relative_to(os.path.realpath(folder)) for folder in folders):
    return 0
  _deny(f'{rule}; {file_path} resolves to {resolved}, outside both.')
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except Exception as error:
    print(f'read gate failed, denying the call: {error}', file=sys.stderr)
    sys.exit(2)
