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
from bro.mcp import (
  ToolLayer,
  allow_commands as _allow_commands,
  block as _block,
  harness,
  serve as _serve,
)

HARNESS = 'claude'

# reading, searching, and modifying the workspace
FILES = ('Read', 'Write', 'Edit', 'NotebookEdit', 'Glob', 'Grep')
_TASK_CONTROL = ('TaskOutput', 'TaskStop')
# running commands, and the job control that reaches the commands already running
SHELL = ('Bash', 'BashOutput', 'KillShell', 'Monitor', *_TASK_CONTROL)
# starting work in another agent — outside the framework's own summon path, so
# outside its container isolation, credential scoping, and recording. the
# spawner has shipped under both `Task` and `Agent`; naming both costs nothing
# and a denylist that misses the live name grants the capability back
DELEGATION = ('Task', 'Agent', 'Workflow')
# the stream of the session's own summon transitions — the one command a
# session that may summon is told to keep `Monitor` on (bro/prompts/summoner.md)
SUMMON_WATCH = 'summon watch'


def block(*tool_names: str) -> When[ToolLayer]:
  """withhold Claude's own `tool_names`, on the harness that serves them.

  Blocks are only meaningful where the harness brings native tools, and
  selecting one anywhere else is a declaration error — so the condition is not
  a choice a caller makes.
  """
  return when(harness == HARNESS, _block(*tool_names))


def watch(*commands: str) -> When[ToolLayer]:
  """serve `Monitor` reaching only `commands`, plus the control over what it starts.

  `Monitor` streams a command's output back as notifications, which is the
  harness's one push channel — and its command is a free-form script, so
  serving it plainly is serving a shell. A persona that declares the commands
  it may watch gets the channel and nothing else; the withheld `SHELL` group it
  must also declare is what makes that narrowing worth anything. The task
  control comes back unnarrowed: it names a running task rather than a command,
  and the only tasks the session has are the watches it was admitted to start.
  """
  return when(harness == HARNESS, _allow_commands('Monitor', *commands) | _serve(*_TASK_CONTROL))


def admit_summon_watch(blocked: dict[str, None], narrowed: dict[str, list[str]]) -> None:
  """keep `SUMMON_WATCH` reachable through `Monitor` over whatever a persona withheld.

  A block of `Monitor` hands it back narrowed to that one command, a narrowing
  of `Monitor` gains it, and the task control over what the watch starts comes
  back with it, as `watch` serves it. Applied to the folded selection, after
  the persona's own layers, so a persona that never withheld `Monitor` is left
  as it is.
  """
  if 'Monitor' in blocked:
    del blocked['Monitor']
    narrowed['Monitor'] = []
  if 'Monitor' not in narrowed:
    return
  if SUMMON_WATCH not in narrowed['Monitor']:
    narrowed['Monitor'].append(SUMMON_WATCH)
  for name in _TASK_CONTROL:
    blocked.pop(name, None)
