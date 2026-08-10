"""a workspace's recorded identity — what it is, rather than where it sits.

Written once at creation and read by every later launch, so no consumer derives
a workspace fact for itself.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from bro.workspace.paths import workspace_dir

_METADATA_FILE = 'meta.json'


class WorkspaceKind(StrEnum):
  """how a workspace's tree is materialized, and with it where its sessions run:
  a git worktree of the project on the host, or a clone bind-mounted into a
  docker container."""

  WORKTREE = 'worktree'
  CONTAINER = 'container'


def workspace_branch(name: str) -> str:
  """the branch a new workspace's tree is checked out on."""
  return f'worktree-{name}'


@dataclass(frozen=True)
class WorkspaceMetadata:
  """what a workspace is: its backing, the branch its tree is checked out on,
  and whether a clean exit disposes of it. `bro/reference/cw.md` ("Workspaces")
  owns what each means for a launch."""

  kind: WorkspaceKind
  branch: str
  throwaway: bool = False

  def dump(self) -> dict:
    return {'kind': self.kind.value, 'branch': self.branch, 'throwaway': self.throwaway}

  @classmethod
  def load(cls, data: dict) -> 'WorkspaceMetadata':
    fields = {'kind', 'branch', 'throwaway'}
    if data.keys() != fields:
      raise ValueError(f'unexpected fields: {sorted(data.keys() ^ fields)}')
    return cls(kind=WorkspaceKind(data['kind']), branch=data['branch'], throwaway=data['throwaway'])


def _metadata_file(project: Path, name: str) -> Path:
  return workspace_dir(project, name) / _METADATA_FILE


def is_workspace(project: Path, name: str) -> bool:
  return _metadata_file(project, name).is_file()


def read_metadata(project: Path, name: str) -> WorkspaceMetadata:
  """the recorded metadata of workspace `name`; raises when it has none."""
  file = _metadata_file(project, name)
  try:
    data = json.loads(file.read_text())
  except FileNotFoundError as e:
    raise ValueError(f'workspace not found: {name}') from e
  return WorkspaceMetadata.load(data)


def write_metadata(project: Path, name: str, metadata: WorkspaceMetadata) -> None:
  """record `metadata`, creating the workspace directory — the file's presence is
  what makes the directory a workspace, so it is written before anything else
  lands there."""
  file = _metadata_file(project, name)
  file.parent.mkdir(parents=True, exist_ok=True)
  file.write_text(json.dumps(metadata.dump(), indent=2))
