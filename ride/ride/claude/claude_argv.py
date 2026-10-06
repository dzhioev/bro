"""the claude argv for a `ride solo|along` session.

Every session runs the full Claude Code harness with the ride-injected append
prompt and its bro's session-local MCP namespaces mounted. Model, the merged
`--settings` (fastMode + statusLine + attribution), `--effort`, the forwarded
claude args, and prompt seeding are handled once, identically wherever the
session runs. Model, effort and fast mode come off the session's claude-code
`LLMSpec` (`SessionRun.llm_spec`).
"""

import contextlib
import json
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from ride.claude.assembly import persona_servers
from ride.claude.harness import CLAUDE, llm_spec
from ride.claude.mcp import MCPEndpoint, http_mcp_config
from ride.claude.statusline import REFRESH_SECONDS, statusline_command
from ride.claude.system_prompt import session_append_prompt
from ride.claude.waiter_state import WAITER_EVENTS, WAITER_MARK

if TYPE_CHECKING:
  from ride.do_ride import SessionRun
  from ride.session import SessionSpec


def _settings_command(module: str, *args: str) -> str:
  """a `--settings` command line running `module` under the runner's interpreter.

  claude runs the string through a shell (hence the quoting) whose PATH excludes
  the project environment, and the wheel format guarantees no mode for a
  packaged file outside `.data/scripts` — so a settings command names the
  interpreter, never a console script or a packaged file's own path. `-m`
  re-executes `module` as `__main__`, so only a leaf nothing else imports may be
  named: a second copy breaks every identity check against its symbols.
  """
  return shlex.join([sys.executable, '-m', module, *args])


def _tool_gate_hooks(narrowed: dict[str, tuple[str, ...]]) -> dict:
  """the `hooks` settings block gating each narrowed native tool to its commands."""
  return {
    'PreToolUse': [
      {
        'matcher': tool,
        'hooks': [
          {
            'type': 'command',
            'command': _settings_command('ride.claude.watch_guard', tool, *commands),
          }
        ],
      }
      for tool, commands in narrowed.items()
    ]
  }


WAITER_TIMEOUT_SECONDS = 24 * 60 * 60


def watch_waiter_hooks() -> dict:
  """the `hooks` settings block waking the session with its watches' lines."""
  waiter_command = _settings_command('ride.claude.watch_waiter', str(WAITER_TIMEOUT_SECONDS))
  waiter = {
    'type': 'command',
    # the mark precedes the interpreter, so a waiter that fails to start
    # reports as the waiter too
    'command': f'echo {shlex.quote(WAITER_MARK)}; exec {waiter_command}',
    'asyncRewake': True,
    'rewakeSummary': 'watch lines',
    'rewakeMessage': "New lines from this session's watches:",
    'timeout': WAITER_TIMEOUT_SECONDS,
  }
  return {event: [{'hooks': [waiter]}] for event in WAITER_EVENTS}


@dataclass(frozen=True)
class ClaudeLaunch:
  """a built claude invocation: the argv, everything after the `claude` program
  token."""

  argv: list[str]
  # a solo session's prompt, delivered as its first stream-json message; an
  # interactive session seeds its prompt through the argv instead
  prompt: Optional[str] = None


# print mode over stream-json: the prompt travels as the first user message
# rather than in the argv, and the stream carries the hook events
# `ride.claude.interrupt.run_streaming` reads a waiter's exits off
STREAM_JSON_ARGS = (
  '-p',
  '--input-format',
  'stream-json',
  '--output-format',
  'stream-json',
  '--verbose',
  '--include-hook-events',
)

# claude's built-in attribution, all off — an empty string is its "omit" value
# for the commit trailer and the pull-request line.
_ATTRIBUTION = {'commit': '', 'pr': '', 'sessionUrl': False}


def build_claude_launch(
  spec: 'SessionSpec | SessionRun',
  *,
  claude_args: list[str],
  endpoint: MCPEndpoint,
) -> ClaudeLaunch:
  """build the claude argv for a session.

  `claude_args` is the forwarded tail (the user's extra args, plus any resolved
  `--resume <id>` — resolution is the caller's, since it differs per launch
  layer). `endpoint` is the session-local MCP server's (the caller owns the
  server lifecycle); every session mounts its bro's claude-harness namespaces
  from it.
  """
  from bro.registry import create_bro

  llm = llm_spec(spec)
  settings: dict = {
    'fastMode': llm.fast_mode,
    'statusLine': {
      'type': 'command',
      'command': statusline_command(),
      'refreshInterval': REFRESH_SECONDS,
    },
    'attribution': _ATTRIBUTION,
  }
  argv = ['--model', llm.model]
  bro = create_bro(spec.bro)
  with contextlib.ExitStack() as server_stack:
    servers = persona_servers(bro)
    for server in servers:
      server_stack.callback(server.close)
    namespaces = list(dict.fromkeys(server.namespace for server in servers))
  blocked_tool_names = bro.blocked_tool_names(CLAUDE)
  narrowed_tool_commands = bro.narrowed_tool_commands(CLAUDE)
  hooks = watch_waiter_hooks()
  if len(narrowed_tool_commands) > 0:
    hooks.update(_tool_gate_hooks(narrowed_tool_commands))
  settings['hooks'] = hooks
  mcp_config = http_mcp_config(namespaces, port=endpoint.port, token=endpoint.token)
  argv += [
    '--disallowed-tools',
    ','.join(('mcp__claude_ai_*', *blocked_tool_names)),
    '--settings',
    json.dumps(settings, separators=(',', ':')),
    '--mcp-config',
    mcp_config,
    '--append-system-prompt',
    session_append_prompt(spec.hold, spec.bro),
  ]
  if spec.hold != 'guided':
    argv.append('--dangerously-skip-permissions')
  if llm.effort is not None:
    argv += ['--effort', llm.effort]
  if spec.solo:
    argv += STREAM_JSON_ARGS
  argv += claude_args
  if spec.solo:
    return ClaudeLaunch(argv=argv, prompt=spec.prompt)
  if spec.prompt is not None:
    argv += ['--', spec.prompt]
  return ClaudeLaunch(argv=argv)
