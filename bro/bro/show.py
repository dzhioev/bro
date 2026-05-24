from bro.bro import Bro, McpServerSpec
from llm.mcp import MCPServer


async def format_card(bro: Bro, *, include_system_prompt: bool = False) -> str:
  parts = [f'# {bro.name}', '', bro.description, '']
  parts.extend(_identity_lines(bro))

  if len(bro.data_sources) > 0:
    parts.extend(['', '## Data sources', ''])
    for ds in bro.data_sources:
      parts.append(f'- **{ds.name}** — {ds.summary}')

  if len(bro._declared_mcp) > 0:
    parts.extend(['', '## MCP servers', ''])
    for spec, server in bro._declared_mcp:
      parts.extend(await _format_mcp_entry(spec, server))

  if include_system_prompt:
    parts.extend(['', '## System prompt', '', '```', bro.system_prompt, '```'])

  return '\n'.join(parts) + '\n'


def _identity_lines(bro: Bro) -> list[str]:
  lines = [f'- model: `{bro.model}`']
  if bro.reasoning_effort is not None:
    lines.append(f'- reasoning effort: `{bro.reasoning_effort}`')
  if bro.web_search:
    lines.append('- web search: enabled (OpenAI hosted)')
  return lines


async def _format_mcp_entry(spec: McpServerSpec, server: MCPServer) -> list[str]:
  label = f'{spec.factory.__module__}.{spec.factory.__qualname__}'
  try:
    tools = await server.list_tools()
  except Exception as e:
    return [f'- `{label}` — failed to list tools: {e}']

  badge = _tool_count_badge(len(tools), filtered=spec.allowed_tools is not None)
  lines = [f'- `{label}` — {badge}']
  for tool in tools:
    lines.append(f'  - `{tool.name}` — {_one_line(tool.description)}')
  return lines


def _tool_count_badge(count: int, *, filtered: bool) -> str:
  noun = 'tool' if count == 1 else 'tools'
  return f'{count} {noun} (filtered)' if filtered else f'{count} {noun}'


def _one_line(text: str, max_len: int = 120) -> str:
  first = text.strip().split('\n', 1)[0].strip()
  if len(first) > max_len:
    return first[: max_len - 1].rstrip() + '…'
  return first
