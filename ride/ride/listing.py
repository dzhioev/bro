import concurrent.futures
import datetime
from typing import Optional

import humanize

from ride.session import harness_for_workspace
from ride.workspace.docker import running_mounts
from ride.workspace.metadata import WorkspaceKind
from ride.workspace.model import Workspace

_ABANDONED = 'abandoned'
_BADGES = {WorkspaceKind.WORKTREE: '[.]', WorkspaceKind.CONTAINER: '[o]', _ABANDONED: '[x]'}
_STATE_ORDER = {WorkspaceKind.WORKTREE: 0, WorkspaceKind.CONTAINER: 1, _ABANDONED: 2}


def _format_age(mtime: float) -> str:
  delta = datetime.timedelta(seconds=int(datetime.datetime.now().timestamp() - mtime))
  return humanize.naturaltime(delta)


def _truncate(s: str, n: int) -> str:
  return s if len(s) <= n else s[: n - 1] + '…'


def list_workspaces() -> int:
  workspaces = Workspace.all()
  containers = [workspace for workspace in workspaces if workspace.kind is WorkspaceKind.CONTAINER]

  def _read(workspace: Workspace) -> tuple[Workspace, Optional[str], Optional[float]]:
    return (
      workspace,
      harness_for_workspace(workspace).read_subject(workspace),
      workspace.last_active(),
    )

  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(running_mounts) if len(containers) > 0 else None
    read_futures = [pool.submit(_read, workspace) for workspace in workspaces]
    mounts = mounts_future.result() if mounts_future is not None else set()
    reads = [future.result() for future in read_futures]

  if len(reads) == 0:
    return 0

  entries: list[tuple[str, Workspace, Optional[str], Optional[float]]] = []
  for workspace, subject, last in reads:
    state = workspace.kind if workspace.is_active(mounts) else _ABANDONED
    entries.append((state, workspace, subject, last))

  entries.sort(key=lambda e: (_STATE_ORDER[e[0]], e[1].kind, e[1].name))
  name_width = max(len(workspace.name) for _, workspace, _, _ in entries)
  attachments = [
    str(workspace.repo) if workspace.repo is not None else '(detached)'
    for _, workspace, _, _ in entries
  ]
  attachment_width = max(len(_truncate(attachment, 50)) for attachment in attachments)
  ages = [_format_age(mtime) if mtime is not None else '' for _, _, _, mtime in entries]
  age_width = max(len(age) for age in ages) if len(ages) > 0 else 0
  for (state, workspace, subject, _), age, attachment in zip(
    entries, ages, attachments, strict=True
  ):
    badge = _BADGES[state]
    age_column = f'  {age:<{age_width}}' if len(age) > 0 else ' ' * (age_width + 2)
    attachment_column = f'  {_truncate(attachment, 50):<{attachment_width}}'
    subject_column = '' if subject is None else f'  {_truncate(subject, 80)}'
    print(f'{badge} {workspace.name:<{name_width}}{age_column}{attachment_column}{subject_column}')
  return 0
