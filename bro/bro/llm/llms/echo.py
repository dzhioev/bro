from dataclasses import dataclass
from typing import ClassVar

import llm.llm
from llm.mcp import MCPServer
from llm.observer import Observer


@dataclass(frozen=True)
class LLMSpec(llm.llm.LLMSpec):
  """trivial spec for Echo. inherits the raising base `.fast` since echo has
  no fast-mode equivalent."""

  TYPE: ClassVar[str] = 'echo'

  model: str = 'echo'

  def create_llm(
    self,
    mcp_servers: list[MCPServer] | None = None,
    observer: Observer | None = None,
  ) -> llm.llm.LLM:
    return Echo.create(mcp_servers=mcp_servers, observer=observer)

  def dump(self) -> dict:
    return {'type': self.TYPE, 'model': self.model}

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    return cls(model=data['model'])


class Echo(llm.llm.LLM):
  @staticmethod
  def create(mcp_servers: list[MCPServer] | None = None, observer: Observer | None = None):
    return Echo(mcp_servers=mcp_servers, observer=observer)

  def __init__(self, mcp_servers: list[MCPServer] | None = None, observer: Observer | None = None):
    super().__init__(mcp_servers, observer=observer)

  async def send(self, messages: list[dict]) -> str:
    if len(messages) == 0:
      return ''
    last = messages[-1]
    content = last.get('content', '')
    if isinstance(content, str):
      return content
    if isinstance(content, list):
      texts = [p.get('text', '') for p in content if p.get('type') == 'text']
      return '\n'.join(texts)
    return str(content)
