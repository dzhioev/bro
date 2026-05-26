import asyncio
import json
from abc import ABC
from pathlib import Path
from typing import Callable

import llm.mcp
from bro.datasources.base import DataSource
from llm.llm import LLM, get_llm
from llm.tracer import BoringTracer, NullTracer, Tracer

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


class BroRaised(llm.mcp.ToolControlSignal):
  """raised by the `raise` service tool to abort a Bro run."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


def _raise(reason: str) -> str:
  raise BroRaised(reason)


_RAISE_DESCRIPTION = (
  'abort the run because the request cannot be fulfilled. Call this when '
  'required credentials or API keys are missing, no appropriate tool or data '
  'source is available, the request contains contradictory constraints, the '
  'input is unclear or cannot be understood (gibberish, ambiguous, or missing '
  'the context needed to act), or any other blocker prevents completing the '
  'task. Do NOT reply with a clarifying question — there is no follow-up turn; '
  'raise instead. Pass a clear, specific reason — it surfaces to the caller as '
  'the failure cause.'
)


def _build_service_server() -> llm.mcp.MCPServer:
  return llm.mcp.InProcessMCPServer(
    [
      llm.mcp.FunctionTool(_raise, name='raise', description=_RAISE_DESCRIPTION),
    ]
  )


_NON_INTERACTIVE_NOTE = (
  'You are running in non-interactive mode — this is a one-shot invocation '
  'with no follow-up turn. If you cannot fulfill the request (missing '
  'credentials, no appropriate tool or data source, contradictory '
  'constraints, the input is unclear or cannot be understood, or any other '
  'blocker), call the `raise` tool with a clear reason instead of producing '
  'a partial or speculative answer or asking a clarifying question — there '
  'is no one to answer it.'
)


McpServerEntry = llm.mcp.MCPServer | Callable[[], llm.mcp.MCPServer]


def _materialize(entry: McpServerEntry) -> llm.mcp.MCPServer:
  return entry if isinstance(entry, llm.mcp.MCPServer) else entry()


class Bro(ABC):
  name: str
  description: str
  model: str = DEFAULT_MODEL
  reasoning_effort: str | None = None
  data_sources: list[DataSource] = []
  mcp_servers: list[McpServerEntry] = []

  _llm: LLM | None = None

  def __init__(self, system_prompt: str = ''):
    self._declared_mcp: list[llm.mcp.MCPServer] = [_materialize(e) for e in type(self).mcp_servers]
    self._mcp_servers: list[llm.mcp.MCPServer] = list(self._declared_mcp)
    for ds in self.data_sources:
      self._mcp_servers.append(ds.as_mcp_server())
    self._service_server: llm.mcp.MCPServer = _build_service_server()
    self._llm = None
    # default to no-op; Bro.run() swaps in a real tracer per invocation so the
    # LLM construction path picks it up via self._tracer.
    self._tracer: Tracer = NullTracer()
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

  async def run(self, input: str, tracer: Tracer | None = None) -> str:
    # caller-supplied tracer wins (CLIs use this to force --boring); otherwise
    # _make_tracer() picks the default. set on self before _create_llm so the
    # LLM construction path can pick it up.
    self._tracer = tracer if tracer is not None else self._make_tracer()
    llm = self._create_llm(interactive=False)
    messages = [
      {'role': 'system', 'content': self._system_prompt_for(interactive=False)},
      {'role': 'user', 'content': input},
    ]
    return await llm.send(messages)

  async def send(self, message: str) -> str:
    if self._llm is None:
      self._llm = self._create_llm(interactive=True)
      messages = [
        {'role': 'system', 'content': self._system_prompt_for(interactive=True)},
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

  def _mcp_servers_for(self, *, interactive: bool) -> list[llm.mcp.MCPServer]:
    # the `raise` service tool only makes sense in non-interactive runs — when no
    # human is in the loop to negotiate, the agent needs a way to abort. In
    # interactive sessions the agent can just describe any blocker in its reply.
    if interactive:
      return list(self._mcp_servers)
    return [*self._mcp_servers, self._service_server]

  def _system_prompt_for(self, *, interactive: bool) -> str:
    if interactive:
      return self.system_prompt
    return f'{self.system_prompt}\n\n{_NON_INTERACTIVE_NOTE}'

  def _make_tracer(self) -> Tracer:
    return BoringTracer(prefix=self.name)

  def _create_llm(self, *, interactive: bool) -> LLM:
    return get_llm(
      'chat_gpt',
      model=self.model,
      mcp_servers=self._mcp_servers_for(interactive=interactive),
      reasoning_effort=self.reasoning_effort,
      tracer=self._tracer,
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
