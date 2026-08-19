from typing import Optional

from bro.llm.llm import NativeLLMSpec
from bro.llm.llms.echo import LLMSpec as _EchoSpec
from bro.llm.mcp import MCPServer
from bro.llm.observer import Observer
from bro.llm.tracker import Tracker
from bro.native.llm import LLM


def create(
  spec: NativeLLMSpec,
  mcp_servers: Optional[list[MCPServer]] = None,
  observer: Optional[Observer] = None,
  tracker: Optional[Tracker] = None,
  agent: Optional[str] = None,
) -> LLM:
  if not isinstance(spec, _EchoSpec):
    raise TypeError(
      f'expected {_EchoSpec.__module__}.LLMSpec, got {type(spec).__module__}.{type(spec).__name__}'
    )
  return Echo(mcp_servers=mcp_servers, observer=observer, tracker=tracker, agent=agent)


class Echo(LLM):
  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    if len(messages) == 0:
      return ''
    last = messages[-1]
    content = last.get('content', '')
    if isinstance(content, str):
      return content
    if isinstance(content, list):
      texts = [part.get('text', '') for part in content if part.get('type') == 'text']
      return '\n'.join(texts)
    return str(content)
