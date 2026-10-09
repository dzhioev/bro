"""Claude `PreToolUse` gate running each `Bash` and `Monitor` command line in brash.

Claude runs a `PreToolUse` hook before the call it matches, handing it the tool's
arguments on stdin. The gate rewrites the call's `command` through `updatedInput`
into one argv — the brash and the policy file named in its own argv, `-c`, and
the line untouched — so Claude's shell reads only single-quoted words and brash
checks the line against the session's command list. A call carrying no command
is denied, as is every call the gate fails on.

One process per call, so it stays stdlib-only and imports nothing of the framework.
"""

import json
import shlex
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
  if len(argv) != 3:
    raise ValueError(f'usage: {argv[0]} BRASH POLICY')
  brash, policy = argv[1], argv[2]
  payload = json.load(sys.stdin)
  tool_input = payload['tool_input']
  command = tool_input.get('command')
  if not isinstance(command, str):
    _deny(
      f'this session runs {payload["tool_name"]} only on a command line, which brash checks '
      'against its command list; a call carrying no command is refused.'
    )
    return 0
  rewritten = {**tool_input, 'command': shlex.join([brash, '--policy', policy, '-c', command])}
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
