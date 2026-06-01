from bro.bro import BaseBro
from llm.mcp import MCPServer


async def format_card(bro: BaseBro, *, include_system_prompt: bool = False) -> str:
  parts = [f'# {bro.name}', '', bro.description, '']
  parts.extend(_identity_lines(bro))

  if len(bro.data_sources) > 0:
    parts.extend(['', '## Data sources', ''])
    for ds in bro.data_sources:
      parts.append(f'- **{ds.name}** — {ds.summary}')

  if len(bro._declared_mcp) > 0:
    parts.extend(['', '## MCP servers', ''])
    for server in bro._declared_mcp:
      parts.extend(await _format_mcp_entry(server))

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


async def _format_mcp_entry(server: MCPServer) -> list[str]:
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
