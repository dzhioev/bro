"""the managed-session facts a trail carries whichever harness records it, read
off the environment the session's launcher published."""

import os
from dataclasses import dataclass
from typing import Any, Optional

from bro.base.git_url import is_git_url, is_network_git_url, sanitize_git_url
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
  repo: Optional[str]
  repo_url: Optional[str]
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
  def git(self) -> Optional[dict[str, Any]]:
    """the trail header's `git`, or None for a detached session."""
    if self.repo is None:
      return None
    repository = sanitize_git_url(self.repo) if is_git_url(self.repo) else self.repo
    git = {'repo': repository, 'branch': self.branch, 'base_sha': self.base_sha}
    if self.repo_url is not None and is_network_git_url(self.repo_url):
      git['url'] = sanitize_git_url(self.repo_url)
    return git


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
  repo = os.environ.get('RIDE_REPO')
  repo_url = os.environ.get('RIDE_REPO_URL')
  branch = os.environ.get(BRANCH_ENV)
  base_sha = os.environ.get(BASE_SHA_ENV)
  attached = (repo, branch, base_sha)
  if any(value is not None for value in attached) and any(value is None for value in attached):
    raise ValueError(f'RIDE_REPO, {BRANCH_ENV} and {BASE_SHA_ENV} are published together')
  if repo_url is not None and repo is None:
    raise ValueError('RIDE_REPO_URL is published only with RIDE_REPO')
  return ManagedSession(
    workspace=_required('RIDE_WORKSPACE'),
    host=_required('RIDE_HOST'),
    host_workspace=_required('RIDE_HOST_WORKSPACE'),
    boxed=isolation == 'boxed',
    ride_command=_required('RIDE_COMMAND'),
    repo=repo,
    repo_url=repo_url,
    branch=branch,
    base_sha=base_sha,
  )
