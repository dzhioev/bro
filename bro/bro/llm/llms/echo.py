import llm.llm
from llm.mcp import MCPServer
from llm.tracer import Tracer


class Echo(llm.llm.LLM):
  @staticmethod
  def create(mcp_servers: list[MCPServer] | None = None, tracer: Tracer | None = None):
    return Echo(mcp_servers=mcp_servers, tracer=tracer)

  def __init__(self, mcp_servers: list[MCPServer] | None = None, tracer: Tracer | None = None):
    super().__init__(mcp_servers, tracer=tracer)

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
