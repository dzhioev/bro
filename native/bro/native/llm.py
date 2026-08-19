#!/usr/bin/env python

import asyncio
import dataclasses
from abc import ABC, abstractmethod
from typing import Optional

import bro.base.args as base_args
from bro.llm.llm import NativeLLMSpec
from bro.llm.mcp import MCPServer, ToolRegistry
from bro.llm.observer import NullObserver, Observer
from bro.llm.tracker import NullTracker, Tracker


class LLM(ABC):
  def __init__(
    self,
    mcp_servers: Optional[list[MCPServer]] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
  ):
    self.tools = ToolRegistry(mcp_servers if mcp_servers is not None else [])
    self.observer: Observer = observer if observer is not None else NullObserver()
    self.tracker: Tracker = tracker if tracker is not None else NullTracker()
    self.agent = agent

  @abstractmethod
  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str: ...

  def cumulative_usage(self) -> Optional[dict[str, dict[str, int]]]:
    return None


async def llm_main(request: str, provider: str, model: Optional[str], attachments: list[str]):
  from bro.llm import providers as llm_providers
  from bro.native import providers as native_providers

  spec = llm_providers.default_spec(provider)
  if not isinstance(spec, NativeLLMSpec):
    raise ValueError(f'provider {provider!r} builds no in-process client')
  if model is not None:
    spec = dataclasses.replace(spec, model=llm_providers.resolve_model(provider, model))
  instance = native_providers.create(spec)
  content: list[dict] = [{'type': 'text', 'text': request}]
  for path in attachments:
    content.append({'type': 'image_url', 'image_url': {'url': path}})
  messages: list[dict] = [{'role': 'user', 'content': content}]
  print(f'> {request}')
  print()
  response = await instance.send(messages)
  print(f'< {response}')


def main(argv: list[str]) -> Optional[int]:
  from bro.llm import providers as llm_providers

  parser = base_args.Parser(description='chat with LLM')
  parser.add_argument('--attach', '-a', dest='attachments', nargs='*', default=[])
  parser.add_argument(
    '--provider', '-p', choices=llm_providers.known_names(), default='echo', help='LLM provider'
  )
  parser.add_argument(
    '--model', '-m', default=None, help='model short name or id within --provider'
  )
  parser.add_argument('request')
  return asyncio.run(llm_main(**parser.parse(argv)))
