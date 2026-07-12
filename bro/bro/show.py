from bro.bro import BaseBro
from llm.mcp import MCPServerSpec, render_text


async def format_card(bro: BaseBro, *, include_system_prompt: bool = False) -> str:
  parts = [f'# {bro.name}', '', bro.description, '']
  parts.extend(_identity_lines(bro))

  if len(bro._data_sources) > 0:
    parts.extend(['', '## Data sources', ''])
    for ds in bro._data_sources:
      declared = set(ds.needed_secrets) | set(ds.optional_secrets)
      summary = render_text(ds.summary, creds=declared)
      parts.append(f'- **{ds.name}** — {summary}')

  if len(bro._mcp_specs) > 0:
    parts.extend(['', '## MCP servers', ''])
    for spec in bro._mcp_specs:
      parts.extend(await _format_mcp_entry(spec))

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
      '- _session baselines (`trails`, `session_log`; `anthropic` for `--bro`) added per-surface_'
    )

  skills = bro.skill_descriptions()
  if len(skills) > 0:
    parts.extend(['', '## Skills', ''])
    for name, description in skills:
      parts.append(f'- **{name}** — {_one_line(description)}')

  if include_system_prompt:
    parts.extend(['', '## System prompt', '', '```', bro.system_prompt, '```'])

  return '\n'.join(parts) + '\n'


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


async def _format_mcp_entry(spec: MCPServerSpec) -> list[str]:
  # the card lists the live server's tools, so the spec is materialized here;
  # `bro show` runs on the host where that is cheap.
  try:
    server = spec.build()
  except Exception as e:
    return [f'- failed to build server: {e}']
  label = f'{type(server).__module__}.{type(server).__qualname__}'
  try:
    tools = await server.list_tools()
  except Exception as e:
    return [f'- `{label}` — failed to list tools: {e}']

  noun = 'tool' if len(tools) == 1 else 'tools'
  lines = [f'- `{label}` — {len(tools)} {noun}']
  for tool in tools:
    lines.append(f'  - `{tool.name}` — {_one_line(tool.description)}')
  return lines


def _one_line(text: str, max_len: int = 120) -> str:
  first = text.strip().split('\n', 1)[0].strip()
  if len(first) > max_len:
    return first[: max_len - 1].rstrip() + '…'
  return first
