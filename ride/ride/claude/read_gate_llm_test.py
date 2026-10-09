"""Live probes of the read gate against the pinned Claude Code.

A session whose bro declares no files reads through `Read` only the folders the
gate names from its hook input, which are where the pinned release writes a
backgrounded command's output and the whole of a result too large to inline.
These probes hold that both land there and read back through the gate, and that
the gate denies a workspace file and a link out of either folder. A release that
moves those files or names its hook input otherwise fails here instead of leaving
a session without its own output or reading past the gate.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from bro import mcp
from bro.monitor import encode_project_path
from ride.claude.claude_argv import STREAM_JSON_ARGS, gate_hooks, reach_arguments
from ride.claude.claude_config import _SESSION_SETTINGS_JSON
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
)
from ride.claude.read_gate import TEMP_ROOT_ENV, session_folders

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300
# the shell is unrestricted, so no command gate stands beside the read gate
_REACH = mcp.Reach(brash=mcp.Brash(unrestricted=True))
_SECRET = 'workspace-secret-5d02'


class _Session:
  """a workspace and Claude folder a probe drives the pinned release in, under a
  session id fixed in advance, so the probe knows the gate's folders before the
  session starts."""

  def __init__(self, claude: Path, root: Path) -> None:
    self.claude = claude
    self.id = str(uuid.uuid4())
    self.workspace = root / 'workspace'
    self.workspace.mkdir()
    self.secret = self.workspace / 'secret.txt'
    self.secret.write_text(f'{_SECRET}\n')
    self.config = root / 'config'
    self.config.mkdir()
    (self.config / '.claude.json').write_text(
      json.dumps(
        {
          'hasCompletedOnboarding': True,
          'projects': {str(self.workspace): {'hasTrustDialogAccepted': True}},
        }
      )
    )
    (self.config / 'settings.json').write_text(json.dumps(_SESSION_SETTINGS_JSON))
    temp_root = root / 'tmp'
    temp_root.mkdir()
    self.env = {
      name: value
      for name, value in os.environ.items()
      if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
    }
    self.env.update(
      CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
      CLAUDE_CONFIG_DIR=str(self.config),
      DISABLE_AUTOUPDATER='1',
      **{TEMP_ROOT_ENV: str(temp_root)},
    )
    self.transcript = (
      self.config / 'projects' / encode_project_path(self.workspace) / f'{self.id}.jsonl'
    )
    hook_input = {'session_id': self.id, 'transcript_path': str(self.transcript)}
    with mock.patch.dict(os.environ, {TEMP_ROOT_ENV: str(temp_root)}):
      self.background_folder, self.results_folder = session_folders(hook_input)

  def run(self, prompt: str) -> list[dict[str, Any]]:
    message = {'type': 'user', 'message': {'role': 'user', 'content': prompt}}
    completed = subprocess.run(
      [
        str(self.claude),
        '--model',
        'haiku',
        '--dangerously-skip-permissions',
        '--session-id',
        self.id,
        *reach_arguments(_REACH),
        '--settings',
        json.dumps({'hooks': gate_hooks(_REACH, None)}),
        *STREAM_JSON_ARGS,
      ],
      cwd=self.workspace,
      env=self.env,
      input=json.dumps(message) + '\n',
      capture_output=True,
      text=True,
      timeout=_SESSION_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr
    # the folders were named off the transcript the session was expected to keep
    assert self.transcript.is_file(), sorted(self.config.glob('projects/*/*'))
    return [json.loads(line) for line in completed.stdout.splitlines()]


def _read_calls(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
  """each `Read` call in a session's stream: the path it named, and the result
  the model was handed for it."""
  calls: dict[str, str] = {}
  results: dict[str, str] = {}
  for event in events:
    content = event.get('message', {}).get('content')
    if not isinstance(content, list):
      continue
    for block in content:
      if event['type'] == 'assistant' and block['type'] == 'tool_use' and block['name'] == 'Read':
        calls[block['id']] = block['input']['file_path']
      elif event['type'] == 'user' and block.get('type') == 'tool_result':
        result = block['content']
        results[block['tool_use_id']] = (
          result if isinstance(result, str) else ''.join(part.get('text', '') for part in result)
        )
  return [(calls[identifier], results[identifier]) for identifier in calls]


def _inside(path: str, folder: Path) -> bool:
  return Path(path).resolve().is_relative_to(folder.resolve())


def test_a_backgrounded_commands_output_and_an_oversized_result_read_back_through_the_gate(
  claude: Path, tmp_path: Path
) -> None:
  session = _Session(claude, tmp_path)
  prompt = (
    'Follow these steps in order.\n'
    '1. Run the command `echo background-marker-41c7` with the Bash tool, with '
    'run_in_background set to true. Its result names the file its output is written to.\n'
    '2. Read that output file with the Read tool.\n'
    "3. Run the command `head -c 60000 /dev/zero | tr '\\0' x; echo; echo "
    'oversized-marker-8e95` with the Bash tool, in the foreground. Its output is too large '
    'to show whole, so its result names the file the whole output was saved to.\n'
    '4. Read that saved file with the Read tool.\n'
    'Then reply with the single word done.'
  )

  reads = _read_calls(session.run(prompt))

  background = [result for path, result in reads if _inside(path, session.background_folder)]
  oversized = [result for path, result in reads if _inside(path, session.results_folder)]
  assert any('background-marker-41c7' in result for result in background), reads
  assert any('oversized-marker-8e95' in result for result in oversized), reads


def test_the_gate_denies_a_workspace_file_and_links_out_of_its_folders(
  claude: Path, tmp_path: Path
) -> None:
  session = _Session(claude, tmp_path)
  links = []
  for folder in (session.background_folder, session.results_folder):
    folder.mkdir(parents=True)
    link = folder / 'escape.txt'
    link.symlink_to(session.secret)
    links.append(link)
  paths = [session.secret, *links]
  prompt = (
    'Read each of these files with the Read tool, one Read call per file, in order, and '
    'make no other tool call:\n'
    + '\n'.join(f'- {path}' for path in paths)
    + '\nThen reply with the single word done.'
  )

  events = session.run(prompt)

  reads = _read_calls(events)
  assert [Path(path) for path, _ in reads] == paths
  for path, result in reads:
    assert _SECRET not in result, path
    assert 'Read opens only what Claude Code wrote for it' in result, (path, result)
  assert _SECRET not in json.dumps(events)
