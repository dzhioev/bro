from pathlib import Path

from bro.datasources.base import DataSource
from bro.llm.mcp import FunctionTool, InProcessMCPServer, MCPServer, render_text


class FileSource(DataSource):
  """surface the contents of a static file as a single-tool data source.

  The framework auto-lists the source in the bro's `## Data sources` block
  (using `summary`), and `as_mcp_server()` mounts a `read` tool (wire name
  `<name>-source__read`) that returns the file body; the tool description
  carries `summary` too, so a surface without the data-sources block (a
  ride-session's tool listing) still sees what the doc is and when to read it.
  Use for canonical reference docs the agent should consult on demand.

  One rendering of the body is read by every harness, so the file must be
  surface-neutral: `read` renders `bro.base.template` directives with no surface
  facts — a `#harness`/`#wire`/`#creds` directive raises at read time instead
  of silently picking a branch. Pass `render=False` to serve the file verbatim
  — for a doc whose payload is the directive syntax itself, where rendering
  would choke on the examples.
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
          description=(
            f'return the full contents of the {self.name} reference document — {self.summary}'
          ),
        )
      ],
    )

  def read(self) -> str:
    text = self._path.read_text()
    if self._render:
      return render_text(text)
    return text
