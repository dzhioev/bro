"""the claude argv for a `cw ss` session — one builder for both flavors.

The native/bro fork is confined to here: `--bro` selects a `--bare` claude wired
to the bro's session-local MCP namespaces and api-key auth, native gets the full
harness with the cw-injected append prompt. Everything else — model, the merged
`--settings` (fastMode + apiKeyHelper), `--effort`, the forwarded claude args,
skill surfacing, prompt seeding — is handled once, identically wherever the
session runs.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

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
  `--append-system-prompt` for native) that CW_SESSION_CONTEXT captures."""

  argv: list[str]
  system_prompt: str


def _deployed_mcp_config() -> str:
  """`--mcp-config` json pointing at the deployed flow MCP server (`--mcp http`)."""
  try:
    cfg = credentials.get_json('flow_mcp')
  except credentials.SecretNotFound:
    raise SystemExit('missing flow_mcp secret — run flow/mcp/server/bootstrap_secrets.sh')
  return json.dumps(
    {
      'mcpServers': {
        'flow': {
          'type': 'http',
          'url': cfg['url'],
          'headers': {'Authorization': f'Bearer {cfg["token"]}'},
        },
      },
    },
    separators=(',', ':'),
  )


def build_claude_launch(
  spec: 'SessionSpec',
  *,
  workspace: Path,
  claude_args: list[str],
  endpoint: Optional[MCPEndpoint] = None,
  skills_dir: Optional[Path] = None,
) -> ClaudeLaunch:
  """build the claude argv for a session running in `workspace`.

  `claude_args` is the forwarded tail (the user's extra args, plus any resolved
  `--resume <id>` — resolution is the caller's, since it differs per launch
  layer). `endpoint` is the session-local MCP server's, required for `--bro` and
  `--mcp local` (the caller owns the server lifecycle). `skills_dir` adds
  `--add-dir` for a native themed session's populated skills root.

  the bro flavor (`--bare --strict-mcp-config --tools ''`) runs claude with no
  project/user CLAUDE.md, no host MCP servers, no built-in tools, and only the
  bro's MCP namespaces (`mcp__<ns>__*`); its system prompt is the bro's
  claude_system_prompt (the composition whose tool-name rule matches those
  mounts). auth comes from the `anthropic` secret via the workspace's own
  `setup/print_anthropic_key.sh`, wired as apiKeyHelper in the merged
  `--settings` — reading the key through a helper avoids the "Detected a custom
  API key" prompt that ANTHROPIC_API_KEY would trigger, and `--settings`
  (flagSettings, not project/local) means claude executes it without a
  workspace trust gate.
  """
  settings: dict = {'fastMode': spec.fast}
  argv = ['--model', _CW_MODEL]
  if spec.bro is not None:
    from bro.registry import create_bro

    bro = create_bro(spec.bro)
    namespaces = list(dict.fromkeys(s.namespace for s in bro.claude_bro_mcp_servers()))
    if endpoint is None:
      raise ValueError('--bro requires a session-local MCP endpoint')
    settings['apiKeyHelper'] = str(workspace / 'setup' / 'print_anthropic_key.sh')
    system_prompt = bro.claude_system_prompt
    argv += [
      '--bare',
      '--strict-mcp-config',
      '--mcp-config',
      _http_mcp_config(namespaces, port=endpoint.port, token=endpoint.token),
      '--settings',
      json.dumps(settings, separators=(',', ':')),
      '--system-prompt',
      system_prompt,
      '--tools',
      '',
      '--allowed-tools',
      ','.join(f'mcp__{ns}__*' for ns in namespaces),
    ]
  else:
    system_prompt = _session_append_prompt(spec.auto, os.environ.get('CW_BRO'))
    argv += [
      '--disallowed-tools',
      'mcp__claude_ai_*',
      '--settings',
      json.dumps(settings, separators=(',', ':')),
    ]
    if spec.mcp == 'http':
      argv += ['--mcp-config', _deployed_mcp_config()]
    elif spec.mcp == 'local':
      if endpoint is None:
        raise ValueError('--mcp local requires a session-local MCP endpoint')
      argv += ['--mcp-config', _http_mcp_config(['flow'], port=endpoint.port, token=endpoint.token)]
    if spec.auto:
      argv.append('--dangerously-skip-permissions')
    argv += ['--append-system-prompt', system_prompt]
  if spec.effort is not None:
    argv += ['--effort', spec.effort]
  argv += claude_args
  if skills_dir is not None:
    argv += ['--add-dir', str(skills_dir)]
  if spec.prompt is not None:
    argv += ['--', spec.prompt]
  return ClaudeLaunch(argv=argv, system_prompt=system_prompt)
