"""Probes of the natives the pinned Claude Code serves a session held to its reach.

`--tools` drops a name the pinned release does not serve without a word, and
what a delegated agent reaches is the release's own decision, so a release that
renames a tool or widens a child fails these probes instead of changing a
persona's reach unseen. The `init` probe runs offline, with no credential; the
delegation probe signs in.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bro import mcp
from bro.registry import create_bro, declared_specs
from ride.claude import native_tools
from ride.claude.claude_argv import STREAM_JSON_ARGS, reach_arguments
from ride.claude.claude_config import _SESSION_SETTINGS_JSON
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
)

_SESSION_TIMEOUT_SECONDS = 300
# `init` reports the delegation spawner under its former name
_REPORTED_NAMES = {'Agent': 'Task'}


def _session(tmp_path: Path, *, token: str | None) -> tuple[Path, dict[str, str]]:
  """a workspace carrying a project `.mcp.json`, and the environment of a session
  over the session's own settings."""
  workspace = tmp_path / 'workspace'
  workspace.mkdir()
  (workspace / '.mcp.json').write_text(
    json.dumps({'mcpServers': {'project-server': {'command': sys.executable, 'args': ['-V']}}})
  )
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  (config / 'settings.json').write_text(json.dumps(_SESSION_SETTINGS_JSON))
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  env.update(
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
    # what the first-party API host turns on, which an offline session lacks
    ENABLE_TOOL_SEARCH='true',
  )
  if token is not None:
    env['CLAUDE_CODE_OAUTH_TOKEN'] = token
  return workspace, env


def _init_event(claude: Path, reach: mcp.Reach, tmp_path: Path) -> dict[str, Any]:
  workspace, env = _session(tmp_path, token=None)
  message = {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}
  completed = subprocess.run(
    [
      str(claude),
      *reach_arguments(reach),
      '--dangerously-skip-permissions',
      *STREAM_JSON_ARGS,
    ],
    cwd=workspace,
    env=env,
    input=json.dumps(message) + '\n',
    capture_output=True,
    text=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
  )
  for line in completed.stdout.splitlines():
    event = json.loads(line)
    if event.get('type') == 'system' and event.get('subtype') == 'init':
      return event
  raise AssertionError(f'no init event: {completed.stdout}\n{completed.stderr}')


@pytest.mark.parametrize('persona', sorted(declared_specs()))
def test_init_holds_the_personas_mapped_natives_and_no_project_server(
  claude: Path, tmp_path: Path, persona: str
) -> None:
  reach = create_bro(persona).reach()
  init = _init_event(claude, reach, tmp_path)

  # the release withholds Monitor while telemetry is off, as the session's
  # settings keep it
  expected = {
    _REPORTED_NAMES.get(name, name) for name in native_tools.allowlist(reach) if name != 'Monitor'
  }
  assert set(init['tools']) == expected
  assert init['mcp_servers'] == []


def _child_roster(config: Path) -> set[str]:
  """the tools the session's delegated agents were given, as their transcripts
  record them: the roster each prompt carried and the tools deferred behind
  ToolSearch."""
  transcripts = list((config / 'projects').glob('*/*/subagents/*.jsonl'))
  assert len(transcripts) > 0, 'no delegated agent ran'
  roster: set[str] = set()
  for transcript in transcripts:
    for line in transcript.read_text().splitlines():
      attachment = json.loads(line).get('attachment', {})
      roster.update(tool['name'] for tool in attachment.get('tools', ()))
      if attachment.get('type') == 'deferred_tools_delta':
        roster.update(attachment['addedNames'])
  return roster


@REQUIRES_CLAUDE_CREDENTIAL
def test_a_delegated_agent_gets_no_native_the_allowlist_withholds(
  claude: Path, tmp_path: Path
) -> None:
  reach = mcp.Reach(files=mcp.Files(write=False), delegation=mcp.Delegation())
  workspace, env = _session(tmp_path, token=claude_token())
  prompt = (
    'Use the Agent tool exactly once, with subagent_type general-purpose, run_in_background '
    'false, and the prompt "Reply with the single word done." Then reply with the single word '
    'finished.'
  )
  subprocess.run(
    [
      str(claude),
      '-p',
      '--model',
      'haiku',
      *reach_arguments(reach),
      '--dangerously-skip-permissions',
      prompt,
    ],
    cwd=workspace,
    env=env,
    capture_output=True,
    text=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
    check=True,
  )

  allowlist = native_tools.allowlist(reach)
  allowed = {*allowlist, *(_REPORTED_NAMES.get(name, name) for name in allowlist)}
  roster = _child_roster(tmp_path / 'config')
  assert 'Read' in roster, roster
  assert roster <= allowed, roster - allowed
