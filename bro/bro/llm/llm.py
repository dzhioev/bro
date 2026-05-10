#!/usr/bin/env python

import base.args

from abc import abstractmethod, ABC
import sys
import asyncio
from functools import cached_property
from typing import Callable

from llm.mcp import MCPServer, ToolRegistry


class LLM(ABC):
  def __init__(self, mcp_servers: list[MCPServer] | None = None):
    self.tools = ToolRegistry(mcp_servers if mcp_servers is not None else [])

  @abstractmethod
  async def send(self, messages: list[dict]) -> str: ...


class LazyConstants:
  @cached_property
  def LLMS_BY_TYPE(self) -> dict[str, type[LLM]]:
    import llm.llms

    result = {}

    def register_llm(type: str, constructor: Callable[[], LLM]) -> None:
      assert type not in result
      result[type] = constructor

    register_llm('echo', llm.llms.Echo.create)
    register_llm('chat_gpt', llm.llms.ChatGPT.create)
    return result

  @cached_property
  def LLM_TYPES(self) -> list[str]:
    return list(self.LLMS_BY_TYPE.keys())


LAZY_CONSTANTS: LazyConstants = LazyConstants()


def get_llm(type: str, *args, **kwargs) -> LLM:
  constructor = LAZY_CONSTANTS.LLMS_BY_TYPE.get(type)
  if constructor is None:
    raise ValueError(f'unknown LLM type: {type}')
  return constructor(*args, **kwargs)


async def llm_main(request: str, llm_type: str, attachments: list[str], *args, **kwargs):
  instance = get_llm(llm_type, *args, **kwargs)
  content: list[dict] = [{'type': 'text', 'text': request}]
  for path in attachments:
    content.append({'type': 'image_url', 'image_url': {'url': path}})
  messages: list[dict] = [{'role': 'user', 'content': content}]
  print(f'> {request}')
  print()
  response = await instance.send(messages)
  print(f'< {response}')


def main(argv=None) -> int | None:
  parser = base.args.Parser(description='chat with LLM')
  parser.add_argument('--attach', '-a', dest='attachments', nargs='*', default=[])
  parser.add_argument('--llm-type', '-t', choices=LAZY_CONSTANTS.LLM_TYPES, default='echo')
  parser.add_argument('request')
  return asyncio.run(llm_main(**parser.parse(argv)))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
