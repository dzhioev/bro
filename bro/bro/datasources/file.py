from pathlib import Path

from bro.datasources.base import DataSource
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, render_text


class FileSource(DataSource):
  """surface the contents of a static file as a single-tool data source.

  The framework auto-lists the source in the bro's `## Data sources` block
  (using `summary`), and `as_mcp_server()` mounts a `read` tool (wire name
  `<name>-source__read`) that returns the file body. Use for canonical reference
  docs the bro should consult on demand — e.g. a playbook that's shared between a
  Claude Code session prompt and a bro.

  The body renders `base.template` `#harness` directives for the bro harness —
  every consumer of this tool works through the bro toolset, mirroring served
  skill bodies (a native claude session reads the same file through its own
  injection, rendered `claude`). Pass `render=False` to serve the file verbatim
  — for a doc whose payload is the directive syntax itself, where rendering
  would execute the examples.
  """

  def __init__(self, name: str, summary: str, path: Path, render: bool = True):
    self.name = name
    self.summary = summary
    self._path = path
    self._render = render

  def as_mcp_server(self) -> MCPServer:
    return InProcessMCPServer(
      self.namespace,
      [
        FunctionTool(
          self.read,
          name='read',
          description=f'return the full contents of the {self.name} reference document',
        )
      ],
    )

  def read(self) -> str:
    text = self._path.read_text()
    if self._render:
      return render_text(text, harness='bro')
    return text
