"""the managed-session facts a trail carries whichever harness records it, read
off the environment the session's launcher published."""

import os
from dataclasses import dataclass
from typing import Any, Optional

from bro.monitor import session_dir
from bro.workspace.paths import BASE_SHA_ENV, BRANCH_ENV, ISOLATION_ENV


@dataclass(frozen=True)
class ManagedSession:
  """where and how a managed session runs, as its launcher published it."""

  workspace: str
  host: str
  host_workspace: str
  boxed: bool
  ride_command: str
  branch: Optional[str]
  base_sha: Optional[str]

  @property
  def location(self) -> dict[str, Any]:
    """the trail header's `location`."""
    return {
      'workspace': self.workspace,
      'host': self.host,
      'dir': self.host_workspace,
      'is_container': self.boxed,
    }

  @property
  def git_record(self) -> Optional[dict[str, Any]]:
    """the `git` launch-context record of an attached session, None for a
    detached one."""
    if self.branch is None:
      return None
    return {
      'kind': 'git',
      'subtype': 'state',
      'title': 'git state at launch',
      'fields': {'branch': self.branch, 'base_sha': self.base_sha},
    }


def _required(name: str) -> str:
  value = os.environ.get(name)
  if value is None:
    raise RuntimeError(f'{name} is unset in a managed session')
  return value


def managed_session() -> Optional[ManagedSession]:
  """the managed session this process runs in, or None outside one — an
  in-process run in no session records no location."""
  if session_dir() is None:
    return None
  isolation = _required(ISOLATION_ENV)
  if isolation not in ('boxed', 'unboxed'):
    raise ValueError(f'invalid {ISOLATION_ENV}: {isolation!r}')
  branch = os.environ.get(BRANCH_ENV)
  base_sha = os.environ.get(BASE_SHA_ENV)
  if (branch is None) != (base_sha is None):
    raise ValueError(f'{BRANCH_ENV} and {BASE_SHA_ENV} are published together')
  return ManagedSession(
    workspace=_required('RIDE_WORKSPACE'),
    host=_required('RIDE_HOST'),
    host_workspace=_required('RIDE_HOST_WORKSPACE'),
    boxed=isolation == 'boxed',
    ride_command=_required('RIDE_COMMAND'),
    branch=branch,
    base_sha=base_sha,
  )
