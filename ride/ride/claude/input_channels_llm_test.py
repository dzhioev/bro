"""Live probe of the model-input channels the pinned Claude Code records.

A claude trail has no model-input source beyond Claude Code's transcript, so the
probe runs ride-shaped print sessions through the recorder and holds one
representative of every input channel against the pinned release.
"""

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Generator, Iterable
from pathlib import Path
from typing import Any

import pytest

from bro.trails.local import LocalStore
from bro.trails.record.session import ManagedSession
from ride.claude.claude_argv import STREAM_JSON_ARGS
from ride.claude.claude_config import _SESSION_SETTINGS_JSON
from ride.claude.interrupt import run_streaming
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
  pinned_claude,
)
from ride.claude.system_prompt import session_append_prompt
from ride.claude.trail_recorder import Recorder

pytestmark = REQUIRES_CLAUDE_CREDENTIAL

_SESSION_TIMEOUT_SECONDS = 300.0
_MCP_DESCRIPTION = 'Return the supplied model-input probe value unchanged.'
_MCP_RESULT = 'model-input-tool-result'
_HOOK_CONTEXT = 'model-input-hook-context'
_CLAUDE_INSTRUCTIONS = 'model-input-claude-instructions'
_AGENTS_INSTRUCTIONS = 'model-input-agents-instructions'
_USER_INPUT = (
  'Call mcp__fixture__echo with value model-input-tool-result, then reply exactly MODEL-INPUT-DONE.'
)
_ASSISTANT_REPLY = 'MODEL-INPUT-DONE'

_MCP_SERVER = f"""\
from mcp.server.fastmcp import FastMCP

server = FastMCP("fixture")


@server.tool(description={_MCP_DESCRIPTION!r})
def echo(value: str) -> str:
  return value


server.run(transport="stdio")
"""

_HOOK = f"""\
import json

output = {{
  "hookSpecificOutput": {{
    "hookEventName": "UserPromptSubmit",
    "additionalContext": {_HOOK_CONTEXT!r},
  }}
}}
print(json.dumps(output))
"""


@pytest.fixture(scope='module')
def claude(pytestconfig: pytest.Config) -> Path:
  return pinned_claude(pytestconfig)


@contextlib.contextmanager
def _bounded(seconds: float) -> Generator[None]:
  """Fail a live session instead of leaving the opt-in stage hung."""

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


def _environment(config: Path) -> dict[str, str]:
  environment = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  environment.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
  )
  return environment


def _fixture_files(root: Path) -> tuple[Path, Path]:
  server = root / 'fixture_mcp.py'
  server.write_text(_MCP_SERVER)
  hook = root / 'fixture_hook.py'
  hook.write_text(_HOOK)
  return server, hook


def _claude_config(config: Path, repository: Path, *, agents_flag: bool) -> None:
  config.mkdir()
  state: dict[str, Any] = {
    'hasCompletedOnboarding': True,
    'projects': {str(repository): {'hasTrustDialogAccepted': True}},
  }
  if agents_flag:
    state['cachedGrowthBookFeatures'] = {'tengu_agents_md_mod': True}
  (config / '.claude.json').write_text(json.dumps(state))
  (config / 'settings.json').write_text(json.dumps(_SESSION_SETTINGS_JSON))


def _recorded_messages(
  root: Path,
  claude: Path,
  *,
  instruction_name: str,
  instruction_text: str,
  prompt: str,
  agents_flag: bool,
) -> list[dict]:
  repository = root / 'repository'
  repository.mkdir()
  subprocess.run(['git', 'init', '-q'], cwd=repository, check=True)
  (repository / instruction_name).write_text(instruction_text)
  config = root / 'config'
  _claude_config(config, repository, agents_flag=agents_flag)
  server, hook = _fixture_files(root)
  hook_settings = {
    'hooks': {
      'UserPromptSubmit': [
        {'hooks': [{'type': 'command', 'command': shlex.join([sys.executable, str(hook)])}]}
      ]
    }
  }
  mcp_config = {
    'mcpServers': {
      'fixture': {
        'type': 'stdio',
        'command': sys.executable,
        'args': [str(server)],
      }
    }
  }
  argv = [
    str(claude),
    '--model',
    'haiku',
    '--allowedTools',
    'Read,mcp__fixture__echo',
    '--dangerously-skip-permissions',
    '--max-turns',
    '4',
    '--settings',
    json.dumps(hook_settings, separators=(',', ':')),
    '--mcp-config',
    json.dumps(mcp_config, separators=(',', ':')),
    '--append-system-prompt',
    session_append_prompt('unattended', 'bro'),
    *STREAM_JSON_ARGS,
  ]
  with contextlib.chdir(repository), _bounded(_SESSION_TIMEOUT_SECONDS):
    run = run_streaming(argv, _environment(config), prompt)
  assert run.code == 0 and not run.stopped, run

  transcripts = list((config / 'projects').rglob('*.jsonl'))
  assert len(transcripts) == 1
  store = LocalStore(root / 'trails')
  session = ManagedSession(
    workspace='input-channels-probe',
    host='test-host',
    host_workspace=str(repository),
    boxed=False,
    ride_command='ride solo --harness claude bro',
    branch=None,
    base_sha=None,
  )
  recorder = Recorder(
    transcripts[0].parent,
    store,
    llm={'model': 'haiku'},
    session=session,
    started_after=0.0,
  )
  assert recorder.finalize() is True
  [header] = list(store.iter_trails(harness='claude'))
  return list(store.iter_messages(header['id']))


def _contains(value: Any, text: str) -> bool:
  if isinstance(value, str):
    return text in value
  if isinstance(value, dict):
    return any(_contains(item, text) for item in value.values())
  if isinstance(value, list):
    return any(_contains(item, text) for item in value)
  return False


def _objects(value: Any) -> Iterable[dict]:
  if isinstance(value, dict):
    yield value
    for item in value.values():
      yield from _objects(item)
  elif isinstance(value, list):
    for item in value:
      yield from _objects(item)


def _notice(messages: list[dict], event: str) -> dict:
  matches = [message for message in messages if message.get('event') == event]
  assert len(matches) >= 1, [message.get('event') for message in messages]
  return matches[0]


def _tool_schema(tool: dict) -> dict | None:
  for name in ('schema', 'input_schema', 'inputSchema'):
    schema = tool.get(name)
    if isinstance(schema, dict):
      return schema
  return None


def test_claude_trail_records_every_model_input_channel(tmp_path: Path, claude: Path) -> None:
  messages = _recorded_messages(
    tmp_path,
    claude,
    instruction_name='CLAUDE.md',
    instruction_text=_CLAUDE_INSTRUCTIONS,
    prompt=_USER_INPUT,
    agents_flag=False,
  )

  snapshots = [
    message['content'] for message in messages if message.get('event') == 'prompt_snapshot'
  ]
  snapshot = next(
    content
    for content in snapshots
    if any(
      isinstance(item.get('cliPrefix'), str) and len(item['cliPrefix']) > 0
      for item in _objects(content)
    )
  )
  appended_prompt = session_append_prompt('unattended', 'bro')
  assert _contains(snapshot, appended_prompt)
  system_prompts = [item['systemPrompt'] for item in _objects(snapshot) if 'systemPrompt' in item]
  assert any(
    _contains(system_prompt, appended_prompt)
    and any(text not in appended_prompt for text in _strings(system_prompt))
    for system_prompt in system_prompts
  )
  native_tool_schemas = [
    schema
    for item in _objects(snapshot)
    if item.get('name') == 'Read'
    for schema in [_tool_schema(item)]
    if schema is not None
  ]
  assert any(len(schema) > 0 for schema in native_tool_schemas)

  tool_notices = [
    message
    for message in messages
    if message.get('event') in {'prompt_snapshot', 'deferred_tools_record', 'deferred_tools_delta'}
  ]
  assert any(_contains(message, _MCP_DESCRIPTION) for message in tool_notices)
  assert _contains(_notice(messages, 'instructions'), _CLAUDE_INSTRUCTIONS)
  assert _contains(_notice(messages, 'hook_additional_context'), _HOOK_CONTEXT)
  assert any(
    message.get('type') == 'user_input' and _contains(message, _USER_INPUT) for message in messages
  )
  assert any(
    message.get('type') == 'assistant' and _contains(message, _ASSISTANT_REPLY)
    for message in messages
  )
  assert any(
    message.get('type') == 'tool_result' and _contains(message, _MCP_RESULT) for message in messages
  )


def _strings(value: Any) -> Iterable[str]:
  if isinstance(value, str):
    yield value
  elif isinstance(value, dict):
    for item in value.values():
      yield from _strings(item)
  elif isinstance(value, list):
    for item in value:
      yield from _strings(item)


def test_disabled_remote_flags_keep_agents_md_out_of_model_input(
  tmp_path: Path, claude: Path
) -> None:
  messages = _recorded_messages(
    tmp_path,
    claude,
    instruction_name='AGENTS.md',
    instruction_text=_AGENTS_INSTRUCTIONS,
    prompt='Reply exactly AGENTS-PROBE-DONE without using a tool.',
    agents_flag=True,
  )

  instruction_notices = [message for message in messages if message.get('event') == 'instructions']
  assert not any(_contains(message, _AGENTS_INSTRUCTIONS) for message in instruction_notices)
