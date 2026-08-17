"""claude `PreToolUse` gate admitting only the commands named in its own argv.

Claude runs a `PreToolUse` hook before the call it matches, handing it the tool's
arguments on stdin and taking its verdict over the session's permission mode —
`--dangerously-skip-permissions` included. Silence is consent, so every path that
is not a match denies, this gate's own failures among them.

Matching is on the whole command, exactly: a pipeline, a redirect, or a second
command joined onto a declared one is a different string and is refused.

One process per call, so it stays stdlib-only and imports nothing of the framework.
"""

import json
import sys


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


def main(argv: list[str]) -> int:
  tool, allowed = argv[1], argv[2:]
  if len(allowed) == 0:
    raise ValueError(f'{tool} gate needs at least one allowed command')
  payload = json.load(sys.stdin)
  command = payload.get('tool_input', {}).get('command')
  if isinstance(command, str) and command.strip() in allowed:
    return 0
  listing = ', '.join(f'`{allowed_command}`' for allowed_command in allowed)
  _deny(
    f'this session may run {tool} on {listing} and nothing else — the command '
    'must match exactly, with nothing appended.'
  )
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except Exception as error:
    print(f'watch guard failed, denying the call: {error}', file=sys.stderr)
    sys.exit(2)
