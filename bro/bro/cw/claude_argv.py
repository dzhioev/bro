"""the claude argv for a `cw ss` session — one builder for both flavors.

The cw-session/bro fork is confined to here: `--bro` selects a `--bare` claude
with api-key auth and the bro's own system prompt, a cw-session keeps the full
harness with the cw-injected append prompt. Both mount their bro's session-local
MCP namespaces. Everything else — model, the merged `--settings` (fastMode +
apiKeyHelper + the Stop-hook listener), `--effort`, the forwarded claude args,
skill surfacing, prompt seeding — is handled once, identically wherever the
session runs.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import prompts
from base import credentials
from cw.constants import _CW_MODEL
from cw.mcp import MCPEndpoint, _http_mcp_config
from cw.system_prompt import _session_append_prompt

if TYPE_CHECKING:
  from cw.session import SessionSpec


@dataclass(frozen=True)
class ClaudeLaunch:
  """a built claude invocation: the argv (everything after the `claude` program
  token) plus the session-shaping prompt text (`--system-prompt` for bro,
  `--append-system-prompt` for a cw-session) that CW_SESSION_CONTEXT captures."""

  argv: list[str]
  system_prompt: str


def _listen_hook_config(workspace: Path) -> dict:
  """hooks for the merged `--settings`: `cw.listen` on Stop, by absolute path
  into the workspace's venv (hook commands run with no venv on PATH). riding
  flagSettings binds the hook in both flavors — `--bare` skips project settings
  — and executes it without a workspace trust gate. the timeout bounds a hung
  classifier call; a timed-out or failing hook is non-blocking for claude."""
  return {
    'Stop': [
      {
        'hooks': [
          {
            'type': 'command',
            'command': str(workspace / '.venv' / 'bin' / 'cw.listen'),
            'timeout': 60,
          }
        ]
      }
    ]
  }


def build_claude_launch(
  spec: 'SessionSpec',
  *,
  workspace: Path,
  claude_args: list[str],
  endpoint: MCPEndpoint,
  skills_dir: Optional[Path] = None,
) -> ClaudeLaunch:
  """build the claude argv for a session running in `workspace`.

  `claude_args` is the forwarded tail (the user's extra args, plus any resolved
  `--resume <id>` — resolution is the caller's, since it differs per launch
  layer). `endpoint` is the session-local MCP server's (the caller owns the
  server lifecycle); every session mounts its bro's namespaces from it — the
  bro's own toolset under `--bro`, the persona's claude-harness set in a
  cw-session. `skills_dir` adds `--add-dir` for a cw-session's populated
  skills root.

  the bro flavor (`--bare --strict-mcp-config --tools ''`) runs claude with no
  project/user CLAUDE.md, no host MCP servers, no built-in tools, and only the
  bro's MCP namespaces (`mcp__<namespace>__*`); its system prompt is the bro's
  claude_system_prompt (the composition whose tool-name rule matches those
  mounts). auth comes from the `anthropic` secret via the workspace's own
  `setup/print_anthropic_key.sh`, wired as apiKeyHelper in the merged
  `--settings` — reading the key through a helper avoids the "Detected a custom
  API key" prompt that ANTHROPIC_API_KEY would trigger, and `--settings`
  (flagSettings, not project/local) means claude executes it without a
  workspace trust gate.
  """
  from bro.registry import create_bro

  settings: dict = {'fastMode': spec.fast, 'hooks': _listen_hook_config(workspace)}
  argv = ['--model', _CW_MODEL]
  bro = create_bro(spec.session_bro)
  servers = (
    bro.claude_bro_mcp_servers() if spec.bro is not None else bro.claude_persona_mcp_servers()
  )
  namespaces = list(dict.fromkeys(s.namespace for s in servers))
  mcp_config = _http_mcp_config(namespaces, port=endpoint.port, token=endpoint.token)
  if spec.bro is not None:
    settings['apiKeyHelper'] = str(workspace / 'setup' / 'print_anthropic_key.sh')
    # the mode fragment renders here — appending the raw file would leak its
    # directives — with the --bro surface's facts: bro harness over mcp wire
    fragment = prompts.mode_fragment(
      spec.mode, harness='bro', wire='mcp', creds=credentials.known_names()
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
    system_prompt = _session_append_prompt(spec.mode, spec.session_bro)
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
  if spec.mode != 'guided':
    argv.append('--dangerously-skip-permissions')
  if spec.effort is not None:
    argv += ['--effort', spec.effort]
  argv += claude_args
  if skills_dir is not None:
    argv += ['--add-dir', str(skills_dir)]
  if spec.prompt is not None:
    argv += ['--', spec.prompt]
  return ClaudeLaunch(argv=argv, system_prompt=system_prompt)
