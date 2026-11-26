"""Claude `SessionStart` hook registering the lifetime's launch facts for the
transcript the session starts writing, and opening a resume with its notice.

Claude Code runs it at startup, on a resume, and whenever the session turns to
another transcript, telling it the transcript and why the session started;
it adds the additional context a `resume` start returns ahead of the next user
line, in the transcript and in the request.
The transcript's history is its lines carrying a `uuid`:
Claude Code appends bookkeeping lines without one even to a session closed
before any input.
"""

import json
import sys
from pathlib import Path
from typing import Optional

from bro.monitor import SESSION_DIR_ENV, session_dir
from bro.workspace.launch_facts import Launches, LaunchFacts, resume_notice


def _lines(transcript: Path) -> list[bytes]:
  """the complete lines `transcript` holds, none before Claude Code creates it."""
  try:
    content = transcript.read_bytes()
  except FileNotFoundError:
    return []
  return content.split(b'\n')[:-1]


def _history_extent(lines: list[bytes]) -> int:
  """the line count through the last line of the history a resume continues."""
  for index in range(len(lines) - 1, -1, -1):
    if 'uuid' in json.loads(lines[index]):
      return index + 1
  return 0


def _notice(
  launches: Launches, earlier: int, record: str, extent: int, facts: LaunchFacts
) -> Optional[str]:
  index = launches.baseline(record, extent)
  if index is None:
    return resume_notice(facts, None)
  if index >= earlier:
    return None
  return resume_notice(facts, launches.entries[index].facts)


def main(argv: list[str]) -> int:
  if len(argv) != 3:
    raise ValueError(f'usage: {argv[0]} EARLIER_LAUNCHES LAUNCH_FACTS_JSON')
  earlier = int(argv[1])
  facts = LaunchFacts.from_wire(json.loads(argv[2]))
  payload = json.load(sys.stdin)
  transcript = payload['transcript_path']
  if not isinstance(transcript, str) or not Path(transcript).is_absolute():
    raise ValueError(f'malformed transcript path: {transcript!r}')
  directory = session_dir()
  if directory is None:
    raise RuntimeError(f'{SESSION_DIR_ENV} is unset')
  path = Path(transcript)
  lines = _lines(path)
  launches = Launches.load(directory)
  notice = (
    _notice(launches, earlier, path.stem, _history_extent(lines), facts)
    if payload['source'] == 'resume'
    else None
  )
  launches.register(path.stem, len(lines), facts)
  if notice is not None:
    output = {'hookEventName': 'SessionStart', 'additionalContext': notice}
    print(json.dumps({'hookSpecificOutput': output}))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
