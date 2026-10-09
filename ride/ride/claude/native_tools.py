"""Claude Code's own tools, as the pinned release names them, per reach group.

A session passes exactly the natives its reach's groups map to through
`--tools`, plus the loop tools every session gets; everything else Claude ships,
whatever a release adds, stays off until mapped here.
`native_tools_llm_test.py` holds the mapping against the pinned release, whose
`--tools` drops a name it does not serve without a word.
"""

from bro.mcp import Reach

READ = ('Read', 'Glob', 'Grep', 'LSP')
WRITE = ('Write', 'Edit', 'NotebookEdit')
SHELL = ('Bash', 'TaskStop', 'Monitor')
WEB = ('WebFetch', 'WebSearch')
DELEGATION = ('Agent', 'Workflow', 'TaskStop')
# Claude's own skill loader, and the search that reaches the MCP tools
# connecting after an interactive session's first turn has started
LOOP = ('Skill', 'ToolSearch')
# the shell tools that run a command line, gated under a finite command list
COMMAND_TOOLS = ('Bash', 'Monitor')


def allowlist(reach: Reach) -> tuple[str, ...]:
  """the natives a session with `reach` is served."""
  names: list[str] = []
  if reach.files is not None:
    names.extend(READ)
    if reach.files.write:
      names.extend(WRITE)
  if reach.brash is not None:
    names.extend(SHELL)
  if reach.web is not None:
    names.extend(WEB)
  if reach.delegation is not None:
    names.extend(DELEGATION)
  names.extend(LOOP)
  return tuple(dict.fromkeys(names))


def command_gate(reach: Reach) -> dict[str, tuple[str, ...]]:
  """each command tool and the commands a finite command list holds it to; empty
  where the reach declares no shell or an unrestricted one."""
  if reach.brash is None or reach.brash.unrestricted:
    return {}
  return dict.fromkeys(COMMAND_TOOLS, reach.brash.commands)
