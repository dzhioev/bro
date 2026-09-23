"""Claude `UserPromptSubmit` hook attaching a finished `watch-next`'s lines to
the task notification that wakes the model.

Claude Code notifies a finished background command with the path of its output
file, never the output, and runs this hook with the notification as the
prompt. Each notified task whose Bash call ran `watch-next` — found by its
tool-use id in the session transcript — has its output returned as additional
context, labeled with the command and task id.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

from ride.claude.watch_commands import WATCH_NEXT

FAILURE_STATUS = 1

_NOTIFICATION = re.compile(r'<task-notification>.*?</task-notification>', re.DOTALL)
# the line Claude Code appends to a background command's output file when it ends
_EXIT_TRAILER = re.compile(r'\n*\[(?:exited with code [^\]\n]*|killed)\]\n*\Z')


def _field(notification: str, tag: str) -> str | None:
  match = re.search(rf'<{tag}>(.*?)</{tag}>', notification, re.DOTALL)
  return None if match is None else match.group(1)


def _bash_commands(transcript: Path, tool_use_ids: set[str]) -> dict[str, str]:
  """the command of each Bash call in `transcript` whose id is among `tool_use_ids`."""
  commands = {}
  with transcript.open() as entries:
    for line in entries:
      if not any(tool_use_id in line for tool_use_id in tool_use_ids):
        continue
      entry = json.loads(line)
      if entry.get('type') != 'assistant':
        continue
      for block in entry['message']['content']:
        if block.get('type') == 'tool_use' and block.get('name') == 'Bash':
          if block['id'] in tool_use_ids:
            commands[block['id']] = block['input']['command']
  return commands


def context(payload: dict[str, Any]) -> str | None:
  """the lines of every `watch-next` the prompt notifies, or None when it notifies none."""
  outputs: dict[str, tuple[str, str]] = {}
  for notification in _NOTIFICATION.findall(payload['prompt']):
    tool_use_id = _field(notification, 'tool-use-id')
    output_file = _field(notification, 'output-file')
    if tool_use_id is None or output_file is None:
      continue
    task_id = _field(notification, 'task-id')
    if task_id is None:
      raise ValueError(f'the notification for tool use {tool_use_id} carries no task-id')
    outputs[tool_use_id] = (task_id, output_file)
  if len(outputs) == 0:
    return None
  commands = _bash_commands(Path(payload['transcript_path']), set(outputs))
  sections = []
  for tool_use_id, (task_id, output_file) in outputs.items():
    command = commands.get(tool_use_id)
    if command is None or WATCH_NEXT.search(command) is None:
      continue
    lines = _EXIT_TRAILER.sub('\n', Path(output_file).read_text())
    if lines.strip() != '':
      sections.append(f'Lines from `{command}` (task {task_id}):\n{lines}')
  return '\n'.join(sections) if len(sections) > 0 else None


def main(argv: list[str]) -> int:
  del argv
  text = context(json.load(sys.stdin))
  if text is not None:
    output = {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}
    print(json.dumps({'hookSpecificOutput': output}))
  return 0


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except Exception as error:
    print(
      f'watch delivery failed, the notification goes without its lines: {error}', file=sys.stderr
    )
    sys.exit(FAILURE_STATUS)
