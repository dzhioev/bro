"""the claude argv for a `ride solo|along` session.

Every session runs Claude Code with the natives its bro's reach maps to, the
ride-injected append prompt, and its bro's session-local MCP namespaces mounted
as its only MCP servers. Model, the merged `--settings` (fastMode + statusLine +
attribution + hooks), `--effort`, the forwarded claude args, and prompt seeding
are handled once, identically wherever the session runs. Model, effort and fast
mode come off the session's claude-code `LLMSpec` (`SessionRun.llm_spec`).
"""

import contextlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.base import spawn
from bro.brash_policy import finite
from bro.mcp import Reach
from ride.claude import native_tools
from ride.claude.assembly import persona_servers
from ride.claude.harness import llm_spec
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
  return shlex.join([*spawn.module_argv(module), *args])


def _command_gate_hooks(brash_policy: Path) -> dict:
  """the `hooks` settings block running each command tool's line in brash under
  `brash_policy`."""
  gate = _settings_command(
    'ride.claude.command_gate', spawn.console_script('brash'), str(brash_policy)
  )
  return {
    'PreToolUse': [
      {'matcher': tool, 'hooks': [{'type': 'command', 'command': gate}]}
      for tool in native_tools.COMMAND_TOOLS
    ]
  }


def reach_arguments(reach: Reach) -> list[str]:
  """the arguments holding a session to `reach`: exactly its natives, and no MCP
  server beyond the `--mcp-config` the launch passes."""
  return [
    '--tools',
    ','.join(native_tools.allowlist(reach)),
    '--strict-mcp-config',
    # claude.ai connectors reach a session through the account, not the config
    '--disallowed-tools',
    'mcp__claude_ai_*',
  ]


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
  brash_policy: Optional[Path],
) -> ClaudeLaunch:
  """build the claude argv for a session.

  `claude_args` is the forwarded tail (the user's extra args, plus any resolved
  `--resume <id>` — resolution is the caller's, since it differs per launch
  layer). `endpoint` is the session-local MCP server's (the caller owns the
  server lifecycle); every session mounts its bro's claude-harness namespaces
  from it. `brash_policy` is the policy file the caller wrote for the bro's
  finite command list, which the command gate runs each line under; None where
  the bro declares no shell or an unrestricted one.
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
  reach = bro.reach()
  if finite(reach) != (brash_policy is not None):
    raise ValueError(f'{spec.bro} needs a brash policy exactly where its command list is finite')
  hooks = watch_waiter_hooks()
  if brash_policy is not None:
    hooks.update(_command_gate_hooks(brash_policy))
  settings['hooks'] = hooks
  mcp_config = http_mcp_config(namespaces, port=endpoint.port, token=endpoint.token)
  argv += [
    *reach_arguments(reach),
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
