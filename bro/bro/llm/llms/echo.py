import llm.llm
from llm.mcp import MCPServer


class Echo(llm.llm.LLM):
  @staticmethod
  def create(mcp_servers: list[MCPServer] | None = None):
    return Echo()

  def __init__(self):
    self.messages = []

  async def tell(self, messages: list[dict]) -> None:
    self.messages = messages

  async def ask(self) -> str:
    if not self.messages:
      return ''
    last = self.messages[-1]
    content = last.get('content', '')
    if isinstance(content, str):
      return content
    if isinstance(content, list):
      texts = [p.get('text', '') for p in content if p.get('type') == 'text']
      return '\n'.join(texts)
    return str(content)
