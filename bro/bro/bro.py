import asyncio
import json
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import llm.mcp
from bro.datasources.base import DataSource
from llm.llm import LLM, get_llm

DEFAULT_MODEL = 'gpt-5'

_SHARED_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts' / 'shared'


def _load_shared_prompts() -> str:
  if not _SHARED_PROMPTS_DIR.is_dir():
    return ''
  parts = []
  for path in sorted(_SHARED_PROMPTS_DIR.glob('*.md')):
    parts.append(path.read_text().strip())
  return '\n\n'.join(parts)


def _render_data_sources(sources: list[DataSource]) -> str:
  lines = ['## Data sources', '', 'You have access to the following read-only data sources:', '']
  for ds in sources:
    lines.append(f'- **{ds.name}** — {ds.summary}')
  lines.append('')
  lines.append(
    'Each source exposes `<name>-search` and `<name>-fetch` tools. '
    'Pass the original user query to `<name>-fetch` so the source can focus the result.'
  )
  return '\n'.join(lines)


@dataclass
class McpServerSpec:
  factory: Callable[[], llm.mcp.MCPServer]
  allowed_tools: list[str] | None = None


McpServerEntry = McpServerSpec | Callable[[], llm.mcp.MCPServer]


def _materialize(entry: McpServerEntry) -> llm.mcp.MCPServer:
  spec = entry if isinstance(entry, McpServerSpec) else McpServerSpec(entry)
  server = spec.factory()
  if spec.allowed_tools is not None:
    server = llm.mcp.FilteredMCPServer(server, spec.allowed_tools)
  return server


class Bro(ABC):
  name: str
  description: str
  model: str = DEFAULT_MODEL
  reasoning_effort: str | None = None
  data_sources: list[DataSource] = []
  mcp_servers: list[McpServerEntry] = []

  _llm: LLM | None = None

  def __init__(self, system_prompt: str = ''):
    self._mcp_servers: list[llm.mcp.MCPServer] = [_materialize(e) for e in type(self).mcp_servers]
    for ds in self.data_sources:
      self._mcp_servers.append(ds.as_mcp_server())
    self._llm = None
    shared = _load_shared_prompts()
    parts = []
    if len(shared) > 0:
      parts.append(shared)
    if len(system_prompt) > 0:
      parts.append(system_prompt)
    if len(self.data_sources) > 0:
      parts.append(_render_data_sources(self.data_sources))
    self.system_prompt = '\n\n'.join(parts)

  def extend_mcp_servers(self, servers: list[llm.mcp.MCPServer]) -> None:
    self._mcp_servers.extend(servers)

  async def run(self, input: str) -> str:
    llm = self._create_llm()
    messages = [
      {'role': 'system', 'content': self.system_prompt},
      {'role': 'user', 'content': input},
    ]
    return await llm.send(messages)

  async def send(self, message: str) -> str:
    if self._llm is None:
      self._llm = self._create_llm()
      messages = [
        {'role': 'system', 'content': self.system_prompt},
        {'role': 'user', 'content': message},
      ]
    else:
      messages = [{'role': 'user', 'content': message}]
    return await self._llm.send(messages)

  async def map(self, inputs: list[str], max_concurrency: int = 5) -> list[str]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded_run(input: str) -> str:
      async with semaphore:
        return await self.run(input)

    return list(await asyncio.gather(*[bounded_run(x) for x in inputs]))

  def _create_llm(self) -> LLM:
    return get_llm(
      'chat_gpt',
      model=self.model,
      mcp_servers=self._mcp_servers,
      reasoning_effort=self.reasoning_effort,
    )


class Tool(llm.mcp.Tool):
  def __init__(self, bro: Bro):
    self._bro = bro

  @property
  def name(self) -> str:
    return self._bro.name

  @property
  def description(self) -> str:
    return self._bro.description

  @property
  def parameters(self) -> dict:
    return {
      'type': 'object',
      'properties': {
        'input': {
          'type': 'string',
          'description': 'input to send to the agent',
        },
      },
      'required': ['input'],
    }

  async def call(self, arguments: dict) -> str:
    return await self._bro.run(arguments['input'])


class ScatterTool(llm.mcp.Tool):
  def __init__(self, bro: Bro, max_concurrency: int = 5):
    self._bro = bro
    self._max_concurrency = max_concurrency

  @property
  def name(self) -> str:
    return f'{self._bro.name}-scatter'

  @property
  def description(self) -> str:
    return f'{self._bro.description} (parallel over multiple inputs)'

  @property
  def parameters(self) -> dict:
    return {
      'type': 'object',
      'properties': {
        'inputs': {
          'type': 'array',
          'items': {'type': 'string'},
          'description': 'list of inputs to process in parallel',
        },
      },
      'required': ['inputs'],
    }

  async def call(self, arguments: dict) -> str:
    results = await self._bro.map(arguments['inputs'], self._max_concurrency)
    return json.dumps(results)
