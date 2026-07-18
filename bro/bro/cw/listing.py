import concurrent.futures
import datetime
from typing import Optional

import humanize

from cw.claude_config import read_subject
from workspace.docker import running_mounts
from workspace.model import ContainerWorkspace, Workspace
from workspace.paths import project_root

_BADGES = {'L': '[.]', 'C': '[o]', 'X': '[x]'}
_KIND_ORDER = {'L': 0, 'C': 1, 'X': 2}


def _format_age(mtime: float) -> str:
  delta = datetime.timedelta(seconds=int(datetime.datetime.now().timestamp() - mtime))
  return humanize.naturaltime(delta)


def _truncate(s: str, n: int) -> str:
  return s if len(s) <= n else s[: n - 1] + '…'


def list_workspaces() -> int:
  project = project_root()
  workspaces = Workspace.all(project)
  containers = [workspace for workspace in workspaces if isinstance(workspace, ContainerWorkspace)]

  def _read(workspace: Workspace) -> tuple[Workspace, Optional[str], Optional[float]]:
    return workspace, read_subject(workspace), workspace.last_active()

  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(running_mounts) if len(containers) > 0 else None
    read_futures = [pool.submit(_read, workspace) for workspace in workspaces]
    mounts = mounts_future.result() if mounts_future is not None else set()
    reads = [f.result() for f in read_futures]

  if len(reads) == 0:
    return 0

  entries: list[tuple[str, Workspace, Optional[str], Optional[float]]] = []
  for workspace, subject, last in reads:
    active = 'C' if isinstance(workspace, ContainerWorkspace) else 'L'
    kind = active if workspace.is_active(mounts) else 'X'
    entries.append((kind, workspace, subject, last))

  entries.sort(key=lambda e: (_KIND_ORDER[e[0]], isinstance(e[1], ContainerWorkspace), e[1].name))
  displays = [workspace.ref for _, workspace, _, _ in entries]
  name_width = max(len(d) for d in displays)
  ages = [_format_age(mtime) if mtime is not None else '' for _, _, _, mtime in entries]
  age_width = max(len(a) for a in ages) if len(ages) > 0 else 0
  for (kind, workspace, subject, _), age in zip(entries, ages, strict=True):
    badge = _BADGES[kind]
    age_column = f'  {age:<{age_width}}' if len(age) > 0 else ' ' * (age_width + 2)
    if subject is None:
      print(f'{badge} {workspace.ref:<{name_width}}{age_column}')
    else:
      print(f'{badge} {workspace.ref:<{name_width}}{age_column}  {_truncate(subject, 80)}')
  return 0
