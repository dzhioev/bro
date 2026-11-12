"""Claude `PreToolUse` gate running each `Bash` command line in brash.

Claude runs a `PreToolUse` hook before the call it matches, handing it the tool's
arguments on stdin. The gate rewrites the call's `command` through `updatedInput`
into one argv — the brash and the policy file named in its own argv, `-c`, and
the line untouched — so Claude's shell reads only single-quoted words and brash
checks the line against the session's command list. Every call the gate fails on
is denied.

One process per call, so it stays stdlib-only and imports nothing of the framework.
"""

import json
import shlex
import sys


def main(argv: list[str]) -> int:
  if len(argv) != 3:
    raise ValueError(f'usage: {argv[0]} BRASH POLICY')
  brash, policy = argv[1], argv[2]
  payload = json.load(sys.stdin)
  tool_input = payload['tool_input']
  rewritten = {
    **tool_input,
    'command': shlex.join([brash, '--policy', policy, '-c', tool_input['command']]),
  }
  print(
    json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'updatedInput': rewritten}})
  )
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except Exception as error:
    print(f'command gate failed, denying the call: {error}', file=sys.stderr)
    sys.exit(2)
