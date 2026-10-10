"""Claude Code's own tools, as the pinned release names them, per reach group.

A session passes exactly the natives its reach's groups map to through
`--tools`, plus the loop tools every session gets and, where the reach has no
files, a gated `Read`; everything else Claude ships, whatever a release adds,
stays off until mapped here.
`native_tools_llm_test.py` holds the mapping against the pinned release, whose
`--tools` drops a name it does not serve without a word.
"""

from bro.brash_policy import finite
from bro.mcp import Reach

READ = ('Read', 'Glob', 'Grep')
WRITE = ('Write', 'Edit', 'NotebookEdit')
SHELL = ('Bash', 'TaskStop', 'Monitor')
WEB = ('WebFetch', 'WebSearch')
DELEGATION = ('Agent', 'Workflow', 'TaskStop')
# served where the reach has no files, held by `ride.claude.read_gate` to the
# session's own Claude folders
GATED_READ = 'Read'
# Claude's own skill loader, and the search that reaches the MCP tools
# connecting after an interactive session's first turn has started
LOOP = ('Skill', 'ToolSearch')
# the shell tools that run a command line, which run it in brash under a finite
# command list
COMMAND_TOOLS = ('Bash', 'Monitor')


def allowlist(reach: Reach) -> tuple[str, ...]:
  """the natives a session with `reach` is served."""
  names: list[str] = []
  if reach.files is not None:
    names.extend(READ)
    if reach.files.write:
      names.extend(WRITE)
  else:
    names.append(GATED_READ)
  if reach.brash is not None:
    names.extend(SHELL)
  if reach.web is not None:
    names.extend(WEB)
  if reach.delegation is not None:
    names.extend(DELEGATION)
  names.extend(LOOP)
  return tuple(dict.fromkeys(names))


def gated(reach: Reach) -> tuple[str, ...]:
  """the natives a session with `reach` is served behind a `PreToolUse` gate."""
  names: list[str] = []
  if finite(reach):
    names.extend(COMMAND_TOOLS)
  if reach.files is None:
    names.append(GATED_READ)
  return tuple(names)
