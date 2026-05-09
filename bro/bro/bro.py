import asyncio
import json
from abc import ABC

import llm.mcp
from llm.llm import LLM, get_llm

DEFAULT_MODEL = 'gpt-5'


class Bro(ABC):
  name: str
  description: str
  system_prompt: str
  model: str = DEFAULT_MODEL

  def __init__(self, mcp_servers: list[llm.mcp.MCPServer] | None = None):
    self._mcp_servers = mcp_servers if mcp_servers is not None else []

  def mcp_servers(self) -> list[llm.mcp.MCPServer]:
    return self._mcp_servers

  async def run(self, input: str) -> str:
    llm = self._create_llm()
    messages = [
      {'role': 'system', 'content': self.system_prompt},
      {'role': 'user', 'content': input},
    ]
    await llm.tell(messages)
    return await llm.ask()

  async def map(self, inputs: list[str], max_concurrency: int = 5) -> list[str]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded_run(input: str) -> str:
      async with semaphore:
        return await self.run(input)

    return list(await asyncio.gather(*[bounded_run(x) for x in inputs]))

  def _create_llm(self) -> LLM:
    return get_llm('chat_gpt', model=self.model, mcp_servers=self.mcp_servers())


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
