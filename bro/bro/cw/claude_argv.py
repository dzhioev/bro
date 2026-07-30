"""the claude argv for a `cw ss` session — one builder for both flavors.

The cw-session/raw fork is confined to here: `--raw` selects a `--bare` claude
with api-key auth and the bro's own system prompt, a cw-session keeps the full
harness with the cw-injected append prompt. Both mount their bro's session-local
MCP namespaces. Everything else — model, the merged `--settings` (fastMode +
statusLine, plus the apiKeyHelper under `--raw`), `--effort`, the forwarded
claude args, and prompt seeding is handled once, identically wherever the
session runs.
"""

import json
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import prompts
from base import credentials
from base.project_root import PROJECT_ROOT
from cw.constants import _CW_MODEL
from cw.mcp import MCPEndpoint, _http_mcp_config
from cw.system_prompt import _session_append_prompt

if TYPE_CHECKING:
  from cw.session import SessionSpec


@dataclass(frozen=True)
class ClaudeLaunch:
  """a built claude invocation: the argv (everything after the `claude` program
  token) plus the session-shaping prompt text (`--system-prompt` for a raw
  session, `--append-system-prompt` for a cw-session) that CW_SESSION_CONTEXT
  captures."""

  argv: list[str]
  system_prompt: str


# prepended to a raw session's argv-seeded first prompt, which fires before
# claude's async MCP connects complete (cw/mcp.py:_server_entry); rationale for
# the turn-local delivery: reference/cw.md "Session-local MCP serving".
_FIRST_TURN_LAUNCH_NOTE = (
  '[launch note: MCP tools connect asynchronously and may be missing from your '
  'tool list on this first turn. If a needed tool is absent, say it has not '
  'loaded yet and end the turn — the tools arrive within seconds; never write a '
  'tool call or its result as text.]'
)


def build_claude_launch(
  spec: 'SessionSpec',
  *,
  claude_args: list[str],
  endpoint: MCPEndpoint,
) -> ClaudeLaunch:
  """build the claude argv for a session.

  `claude_args` is the forwarded tail (the user's extra args, plus any resolved
  `--resume <id>` — resolution is the caller's, since it differs per launch
  layer). `endpoint` is the session-local MCP server's (the caller owns the
  server lifecycle); every session mounts its bro's namespaces from it — the
  bro's own toolset under `--raw`, the persona's claude-harness set in a
  cw-session.

  the raw flavor (`--bare --strict-mcp-config --tools ''`) runs claude with no
  project/user CLAUDE.md, no host MCP servers, no built-in tools, and only the
  bro's MCP namespaces (`mcp__<namespace>__*`); its system prompt is the bro's
  claude_system_prompt (the composition whose tool-name rule matches those
  mounts). auth comes from the `anthropic` secret via the workspace's own
  `cw/print_anthropic_key.sh`, wired as apiKeyHelper in the merged
  `--settings` — reading the key through a helper avoids the "Detected a custom
  API key" prompt that ANTHROPIC_API_KEY would trigger, and `--settings`
  (flagSettings, not project/local) means claude executes it without a
  workspace trust gate.
  """
  from bro.registry import create_bro

  settings: dict = {
    'fastMode': spec.fast,
    'statusLine': {
      # the runner's own interpreter rather than a PATH lookup for the
      # registered script — the session's PATH need not carry the workspace's
      # venv, and the shell claude runs the command through splits on spaces
      'type': 'command',
      'command': f'{shlex.quote(sys.executable)} -m cw.statusline',
    },
  }
  argv = ['--model', _CW_MODEL]
  bro = create_bro(spec.session_bro)
  servers = bro.claude_bro_mcp_servers() if spec.raw else bro.claude_persona_mcp_servers()
  namespaces = list(dict.fromkeys(s.namespace for s in servers))
  mcp_config = _http_mcp_config(namespaces, port=endpoint.port, token=endpoint.token)
  if spec.raw:
    # PROJECT_ROOT, not the workspace root: the runner executes from the
    # workspace's venv, so this is the workspace's own ppp checkout — the
    # workspace itself for ppp, the ppp submodule for a project vendoring it
    settings['apiKeyHelper'] = str(PROJECT_ROOT / 'cw' / 'print_anthropic_key.sh')
    # the hold fragment renders here — appending the unrendered file would leak
    # its directives — with the --raw surface's facts: bro harness over mcp wire
    fragment = prompts.hold_fragment(
      spec.hold, harness='bro', wire='mcp', creds=credentials.known_names()
    )
    system_prompt = f'{bro.claude_system_prompt}\n\n{fragment}'
    argv += [
      '--bare',
      '--strict-mcp-config',
      '--mcp-config',
      mcp_config,
      '--settings',
      json.dumps(settings, separators=(',', ':')),
      '--system-prompt',
      system_prompt,
      '--tools',
      '',
      '--allowed-tools',
      ','.join(f'mcp__{namespace}__*' for namespace in namespaces),
    ]
  else:
    system_prompt = _session_append_prompt(spec.hold, spec.session_bro)
    argv += [
      '--disallowed-tools',
      'mcp__claude_ai_*',
      '--settings',
      json.dumps(settings, separators=(',', ':')),
      '--mcp-config',
      mcp_config,
      '--append-system-prompt',
      system_prompt,
    ]
  if spec.hold != 'guided':
    argv.append('--dangerously-skip-permissions')
  if spec.effort is not None:
    argv += ['--effort', spec.effort]
  argv += claude_args
  if spec.prompt is not None:
    prompt = spec.prompt
    if spec.raw:
      prompt = f'{_FIRST_TURN_LAUNCH_NOTE}\n\n{prompt}'
    argv += ['--', prompt]
  return ClaudeLaunch(argv=argv, system_prompt=system_prompt)
