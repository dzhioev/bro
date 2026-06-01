from pathlib import Path

from bro.datasources.base import DataSource
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer


class FileSource(DataSource):
  """surface the contents of a static file as a single-tool data source.

  The framework auto-lists the source in the bro's `## Data sources` block
  (using `summary`), and `as_mcp_server()` mounts a `<name>-read` tool that
  returns the file body. Use for canonical reference docs the bro should
  consult on demand — e.g. a playbook that's shared between a Claude Code
  session prompt and a bro.
  """

  def __init__(self, name: str, summary: str, path: Path):
    self.name = name
    self.summary = summary
    self._path = path

  def as_mcp_server(self) -> MCPServer:
    return InProcessMCPServer(
      [
        FunctionTool(
          self.read,
          name=f'{self.name}-read',
          description=f'return the full contents of the {self.name} reference document',
        )
      ]
    )

  def read(self) -> str:
    return self._path.read_text()
