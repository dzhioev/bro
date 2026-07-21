from base import credentials
from bro.bro import BaseBro
from llm.mcp import MCPServerSpec


async def format_card(bro: BaseBro, *, include_system_prompt: bool = False) -> str:
  parts = [f'# {bro.name}', '', bro.description, '']
  parts.extend(_identity_lines(bro))

  if len(bro._data_sources) > 0:
    parts.extend(['', '## Data sources', ''])
    for ds in bro._data_sources:
      parts.append(f'- **{ds.name}** — {ds.rendered_summary()}')

  if len(bro._mcp_specs) > 0:
    parts.extend(['', '## MCP tools', ''])
    parts.extend(await _mcp_tool_lines(bro._mcp_specs))

  if len(bro._features) > 0:
    parts.extend(['', '## Features', ''])
    for name, gates in bro._features.items():
      parts.append(_feature_line(name, gates))

  manifest = bro.needed_secrets()
  optional = bro.optional_secrets()
  llm_secrets = bro.llm_spec.needed_secrets()
  if len(manifest) > 0 or len(optional) > 0 or len(llm_secrets) > 0:
    parts.extend(['', '## Secrets', ''])
    for name in manifest:
      parts.append(f'- `{name}`')
    for name in optional:
      parts.append(f'- `{name}` — optional (used if present)')
    for name in llm_secrets:
      parts.append(f'- `{name}` — LLM key')
    parts.append(
      '- _session baselines (`trails`, `session_log`; `anthropic` for `--raw`) added per-surface_'
    )

  scripts = bro.script_descriptions()
  if len(scripts) > 0:
    parts.extend(['', '## Scripts', ''])
    for name, description in scripts:
      parts.append(f'- **@::{name}** — {_one_line(description)}')

  if include_system_prompt:
    parts.extend(['', '## System prompt', '', '```', bro.system_prompt, '```'])

  return '\n'.join(parts) + '\n'


def _feature_line(name: str, gates: tuple[str, ...]) -> str:
  if len(gates) == 0:
    return f'- **{name}** — always on'
  gate_list = ', '.join(f'`{gate}`' for gate in gates)
  state = 'on' if all(credentials.available(gate) for gate in gates) else 'off'
  return f'- **{name}** — needs {gate_list}; {state} in this environment'


def _identity_lines(bro: BaseBro) -> list[str]:
  # render the bro's LLM spec generically via its `dump()` — each provider's
  # spec carries its own knobs and we don't want this module to know about any
  # one provider. drop the `type` discriminator and any unset (None) field.
  lines = []
  for key, value in bro.llm_spec.dump().items():
    if key == 'type' or value is None:
      continue
    label = key.replace('_', ' ')
    lines.append(f'- {label}: `{value}`')
  return lines


async def _mcp_tool_lines(specs: list[MCPServerSpec]) -> list[str]:
  # the card lists the live servers' tools, so the specs are materialized here;
  # `bro show` runs on the host where that is cheap. tools are grouped under the
  # namespace their wire names carry, not under the server that serves them.
  tool_lines_by_namespace: dict[str, list[str]] = {}
  failures = []
  for spec in specs:
    try:
      server = spec.build()
    except Exception as e:
      failures.append(f'- failed to build server: {e}')
      continue
    try:
      tools = await server.list_tools()
    except Exception as e:
      failures.append(f'- `{server.namespace}` — failed to list tools: {e}')
      continue
    tool_lines_by_namespace.setdefault(server.namespace, []).extend(
      f'  - `{tool.name}` — {_one_line(tool.description)}' for tool in tools
    )
  lines = []
  for namespace, tool_lines in tool_lines_by_namespace.items():
    noun = 'tool' if len(tool_lines) == 1 else 'tools'
    lines.append(f'- `{namespace}` — {len(tool_lines)} {noun}')
    lines.extend(tool_lines)
  return lines + failures


def _one_line(text: str, max_len: int = 120) -> str:
  first = text.strip().split('\n', 1)[0].strip()
  if len(first) > max_len:
    return first[: max_len - 1].rstrip() + '…'
  return first
