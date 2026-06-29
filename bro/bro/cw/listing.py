import concurrent.futures
import datetime
from typing import Optional

import humanize

from cw.docker import running_mounts
from cw.paths import _project_root
from cw.workspace import ContainerWorkspace, Workspace

_BADGES = {'L': '[.]', 'C': '[o]', 'X': '[x]'}
_KIND_ORDER = {'L': 0, 'C': 1, 'X': 2}


def _format_age(mtime: float) -> str:
  delta = datetime.timedelta(seconds=int(datetime.datetime.now().timestamp() - mtime))
  return humanize.naturaltime(delta)


def _truncate(s: str, n: int) -> str:
  return s if len(s) <= n else s[: n - 1] + '…'


def list_workspaces() -> int:
  proj = _project_root()
  workspaces = Workspace.all(proj)
  containers = [ws for ws in workspaces if isinstance(ws, ContainerWorkspace)]

  def _read(ws: Workspace) -> tuple[Workspace, Optional[str], Optional[float]]:
    return ws, ws.subject(), ws.last_active()

  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(running_mounts) if len(containers) > 0 else None
    read_futures = [pool.submit(_read, ws) for ws in workspaces]
    mounts = mounts_future.result() if mounts_future is not None else set()
    reads = [f.result() for f in read_futures]

  if len(reads) == 0:
    return 0

  entries: list[tuple[str, Workspace, Optional[str], Optional[float]]] = []
  for ws, subject, last in reads:
    active = 'C' if isinstance(ws, ContainerWorkspace) else 'L'
    kind = active if ws.is_active(mounts) else 'X'
    entries.append((kind, ws, subject, last))

  entries.sort(key=lambda e: (_KIND_ORDER[e[0]], isinstance(e[1], ContainerWorkspace), e[1].name))
  displays = [ws.ref for _, ws, _, _ in entries]
  name_w = max(len(d) for d in displays)
  ages = [_format_age(mtime) if mtime is not None else '' for _, _, _, mtime in entries]
  age_w = max(len(a) for a in ages) if len(ages) > 0 else 0
  for (kind, ws, subject, _), age in zip(entries, ages, strict=True):
    badge = _BADGES[kind]
    age_col = f'  {age:<{age_w}}' if len(age) > 0 else ' ' * (age_w + 2)
    if subject is None:
      print(f'{badge} {ws.ref:<{name_w}}{age_col}')
    else:
      print(f'{badge} {ws.ref:<{name_w}}{age_col}  {_truncate(subject, 80)}')
  return 0
