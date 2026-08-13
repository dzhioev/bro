"""Claude Code's own tools, named where a persona can reach them.

A persona that declares `block(...)` is naming another product's tool surface,
which moves with Claude versions. Keeping the names here means one site to
follow that drift instead of one per persona, and `block` gates the whole set
behind the harness that has them.

The groups are cut by what a session can *do* with them, since that is what a
persona forgoes: `FILES` reaches the workspace, `SHELL` runs commands in it, and
`DELEGATION` starts work in another agent.
"""

from bro.base.condition import When, when
from bro.llm.mcp import ToolLayer, block as _block, harness

HARNESS = 'claude'

# reading, searching, and modifying the workspace
FILES = ('Read', 'Write', 'Edit', 'NotebookEdit', 'Glob', 'Grep')
# running commands, and the job control that reaches the commands already running
SHELL = ('Bash', 'BashOutput', 'KillShell', 'Monitor', 'TaskOutput', 'TaskStop')
# starting work in another agent — outside the framework's own summon path, so
# outside its container isolation, credential scoping, and recording. the
# spawner has shipped under both `Task` and `Agent`; naming both costs nothing
# and a denylist that misses the live name grants the capability back
DELEGATION = ('Task', 'Agent', 'Workflow')


def block(*tool_names: str) -> When[ToolLayer]:
  """withhold Claude's own `tool_names`, on the harness that serves them.

  Blocks are only meaningful where the harness brings native tools, and
  selecting one anywhere else is a declaration error — so the condition is not
  a choice a caller makes.
  """
  return when(harness == HARNESS, _block(*tool_names))
