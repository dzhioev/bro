"""a workspace's recorded identity and optional repository attachment."""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Optional

from bro.workspace.paths import workspace_dir

_METADATA_FILE = 'meta.json'


class WorkspaceKind(StrEnum):
  WORKTREE = 'worktree'
  CONTAINER = 'container'


def workspace_branch(name: str) -> str:
  return f'worktree-{name}'


@dataclass(frozen=True)
class WorkspaceMetadata:
  kind: WorkspaceKind
  repo: Optional[str]
  branch: Optional[str]
  throwaway: bool = False

  def __post_init__(self) -> None:
    if (self.repo is None) != (self.branch is None):
      raise ValueError('workspace repo and branch must either both be present or both be absent')

  def dump(self) -> dict:
    data: dict = {'kind': self.kind.value, 'throwaway': self.throwaway}
    if self.repo is not None:
      data['repo'] = self.repo
      data['branch'] = self.branch
    return data

  @classmethod
  def load(cls, data: dict) -> 'WorkspaceMetadata':
    required = {'kind', 'throwaway'}
    optional = {'repo', 'branch'}
    if not required <= data.keys() or not data.keys() <= required | optional:
      raise ValueError(f'unexpected fields: {sorted(data.keys() ^ required)}')
    repo = data.get('repo')
    branch = data.get('branch')
    if repo is not None and (not isinstance(repo, str) or repo == ''):
      raise ValueError('workspace repo must be a non-empty string when present')
    if branch is not None and (not isinstance(branch, str) or branch == ''):
      raise ValueError('workspace branch must be a non-empty string when present')
    if not isinstance(data['throwaway'], bool):
      raise ValueError('workspace throwaway must be a bool')
    return cls(
      kind=WorkspaceKind(data['kind']),
      repo=repo,
      branch=branch,
      throwaway=data['throwaway'],
    )


def _metadata_file(name: str) -> Path:
  return workspace_dir(name) / _METADATA_FILE


def is_workspace(name: str) -> bool:
  return _metadata_file(name).is_file()


def read_metadata(name: str) -> WorkspaceMetadata:
  file = _metadata_file(name)
  try:
    data = json.loads(file.read_text())
  except FileNotFoundError as exception:
    raise ValueError(f'workspace not found: {name}') from exception
  return WorkspaceMetadata.load(data)


def write_metadata(name: str, metadata: WorkspaceMetadata) -> None:
  file = _metadata_file(name)
  file.parent.mkdir(parents=True, exist_ok=True)
  file.write_text(json.dumps(metadata.dump(), indent=2))
