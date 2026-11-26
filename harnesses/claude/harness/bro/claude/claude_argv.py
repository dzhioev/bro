"""the claude argv for a `ride solo|along` session.

Every session runs Claude Code with the natives its bro's reach maps to, the
ride-injected append prompt, and its bro's session-local MCP namespaces mounted
as its only MCP servers. Model, the merged `--settings` (fastMode + statusLine +
attribution + hooks, kept on), `--effort`, a resume, and prompt seeding are
handled once, identically wherever the session runs; the permission mode follows
the session's hold and isolation. Model, effort and fast mode come off the
session's anthropic `Recipe` (`SessionRun.recipe`); unset effort is Claude
Code's own default.
"""

import contextlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.base import spawn
from bro.brash_policy import finite
from bro.claude import native_tools
from bro.claude.assembly import persona_servers
from bro.claude.harness import recipe
from bro.claude.mcp import MCPEndpoint, http_mcp_config
from bro.claude.session_end_state import ANSWER_TOOL, RAISE_TOOL
from bro.claude.statusline import REFRESH_SECONDS, statusline_command
from bro.claude.system_prompt import session_append_prompt
from bro.claude.waiter_state import WAITER_EVENTS, WAITER_MARK
from bro.mcp import Reach
from bro.workspace.launch_facts import LaunchFacts
from ride.workspace.metadata import Isolation

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


def gate_hooks(reach: Reach, brash_policy: Optional[Path]) -> dict:
  """the `hooks` settings block gating the natives `reach` is served: each command
  tool's line run in brash under `brash_policy`, the policy of a finite command
  list, and `Read` held to the session's own Claude folders."""
  commands = {native_tools.GATED_READ: _settings_command('bro.claude.read_gate')}
  if brash_policy is not None:
    command_gate = _settings_command(
      'bro.claude.command_gate', spawn.console_script('brash'), str(brash_policy)
    )
    commands.update(dict.fromkeys(native_tools.COMMAND_TOOLS, command_gate))
  gated = native_tools.gated(reach)
  if len(gated) == 0:
    return {}
  return {
    'PreToolUse': [
      {'matcher': tool, 'hooks': [{'type': 'command', 'command': commands[tool]}]} for tool in gated
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
  waiter_command = _settings_command('bro.claude.watch_waiter', str(WAITER_TIMEOUT_SECONDS))
  waiter = {
    'type': 'command',
    # the mark precedes the interpreter, so a waiter that fails to start
    # reports as the waiter too
    'command': f'echo {shlex.quote(WAITER_MARK)}; exec {waiter_command}',
    'asyncRewake': True,
    'rewakeSummary': 'watch wake',
    'rewakeMessage': "New lines from this session's watches:",
    'timeout': WAITER_TIMEOUT_SECONDS,
  }
  return {event: [{'hooks': [waiter]}] for event in WAITER_EVENTS}


def session_end_hooks() -> dict:
  """the `hooks` settings block stopping the turn of a call that ended the session."""
  stop = {'type': 'command', 'command': _settings_command('bro.claude.session_end')}
  return {'PostToolUse': [{'matcher': tool, 'hooks': [stop]} for tool in (ANSWER_TOOL, RAISE_TOOL)]}


def session_start_hooks(earlier_launches: int, launch_facts: LaunchFacts) -> dict:
  """the `hooks` settings block registering `launch_facts` for each transcript the
  session starts writing, after the `earlier_launches` entries of earlier lifetimes."""
  command = _settings_command(
    'bro.claude.session_start',
    str(earlier_launches),
    json.dumps(launch_facts.to_wire(), separators=(',', ':')),
  )
  return {'SessionStart': [{'hooks': [{'type': 'command', 'command': command}]}]}


ACTIVITY_EVENTS = ('UserPromptSubmit', 'PostToolUse', 'Stop')


def activity_hooks(activity_file: Path) -> dict:
  """the `hooks` settings block touching the session's activity mark as it works."""
  # a bare `touch` rather than a `_settings_command`: it runs after every tool
  # call, where an interpreter start would slow the session down
  touch = {'type': 'command', 'command': shlex.join(['touch', str(activity_file)])}
  return {event: [{'hooks': [touch]}] for event in ACTIVITY_EVENTS}


def permission_mode(hold: str, isolation: Isolation) -> str:
  """the permission mode a session's claude starts in."""
  if hold == 'guided':
    # passed rather than left to claude, whose own default starts a TUI in auto mode
    return 'manual'
  return 'bypassPermissions' if isolation is Isolation.BOXED else 'auto'


def permission_settings(isolation: Isolation) -> dict:
  """the settings block a session's permission modes run under: boxed, it
  pre-answers bypass mode's acceptance dialog; unboxed, it turns bypass mode
  off however the session is asked into it."""
  if isolation is Isolation.BOXED:
    return {'skipDangerousModePermissionPrompt': True}
  return {'permissions': {'disableBypassPermissionsMode': 'disable'}}


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
# `bro.claude.interrupt.run_streaming` reads a waiter's exits off
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

# `--settings` outranks a repository's project and local settings, either of which
# may switch every hook off
HOOKS_ON = {'disableAllHooks': False}


def build_claude_launch(
  spec: 'SessionSpec | SessionRun',
  *,
  isolation: Isolation,
  resume_session: Optional[str],
  endpoint: MCPEndpoint,
  brash_policy: Optional[Path],
  activity_file: Path,
  plugin_dir: Optional[Path],
  launch_facts: LaunchFacts,
  earlier_launches: int,
) -> ClaudeLaunch:
  """build the claude argv for a session.

  `isolation` is the session's, which with its hold selects the permission mode.
  `resume_session` is the id of the Claude session a resume continues, which the
  caller resolves from the workspace's transcripts; None for a fresh session.
  `endpoint` is the session-local MCP server's (the caller owns the server
  lifecycle); every session mounts its bro's claude-harness namespaces from it.
  `brash_policy` is the policy file the caller wrote for the bro's finite command
  list, which the command gate runs each line under; None where the bro declares
  no shell or an unrestricted one. `activity_file` is the session's activity mark
  its hooks touch. `plugin_dir` is the session's copy of ride's Claude plugin
  (`bro.claude.claude_plugin`); None for a session that loads none.
  `launch_facts` are the lifetime's, which its `SessionStart` hook registers
  after the `earlier_launches` entries the session's launch-facts file holds.
  """
  from bro.registry import create_bro

  llm = recipe(spec)
  settings: dict = {
    'fastMode': llm.fast_mode,
    'statusLine': {
      'type': 'command',
      'command': statusline_command(),
      'refreshInterval': REFRESH_SECONDS,
    },
    'attribution': _ATTRIBUTION,
    **HOOKS_ON,
    **permission_settings(isolation),
  }
  argv = ['--model', llm.model]
  bro = create_bro(spec.bro)
  with contextlib.ExitStack() as server_stack:
    servers = persona_servers(bro, spec.hold)
    for server in servers:
      server_stack.callback(server.close)
    namespaces = list(dict.fromkeys(server.namespace for server in servers))
  reach = bro.reach()
  if finite(reach) != (brash_policy is not None):
    raise ValueError(f'{spec.bro} needs a brash policy exactly where its command list is finite')
  hooks = {**watch_waiter_hooks(), **gate_hooks(reach, brash_policy)}
  for block in (
    session_end_hooks(),
    activity_hooks(activity_file),
    session_start_hooks(earlier_launches, launch_facts),
  ):
    for event, entries in block.items():
      hooks.setdefault(event, []).extend(entries)
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
    '--permission-mode',
    permission_mode(spec.hold, isolation),
  ]
  if plugin_dir is not None:
    argv += ['--plugin-dir', str(plugin_dir)]
  if llm.effort is not None:
    argv += ['--effort', llm.effort]
  if spec.solo:
    argv += STREAM_JSON_ARGS
  if resume_session is not None:
    argv += ['--resume', resume_session]
  if spec.solo:
    return ClaudeLaunch(argv=argv, prompt=spec.prompt)
  if spec.prompt is not None:
    argv += ['--', spec.prompt]
  return ClaudeLaunch(argv=argv)
