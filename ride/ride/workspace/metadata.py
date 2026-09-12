"""A workspace's recorded identity, isolation, and optional repository attachment."""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Optional

from bro.workspace.paths import is_workspace_name, workspace_dir

_METADATA_FILE = 'workspace.json'
BRANCH_ENV = 'RIDE_BRANCH'


class Isolation(StrEnum):
  BOXED = 'boxed'
  UNBOXED = 'unboxed'


def workspace_branch(name: str) -> str:
  return f'workspace-{name}'


@dataclass(frozen=True)
class WorkspaceMetadata:
  isolation: Isolation
  repo: Optional[str]
  branch: Optional[str]
  throwaway: bool = False
  tree: Optional[str] = None

  def __post_init__(self) -> None:
    if (self.repo is None) != (self.branch is None):
      raise ValueError('workspace repo and branch must either both be present or both be absent')
    if self.tree is not None:
      if self.isolation is not Isolation.UNBOXED:
        raise ValueError('an external workspace tree requires unboxed isolation')
      if self.repo is not None:
        raise ValueError('an external workspace tree requires a detached workspace')
      if not Path(self.tree).is_absolute():
        raise ValueError('workspace tree must be an absolute path')

  def dump(self) -> dict:
    data: dict = {
      'isolation': self.isolation.value,
      'throwaway': self.throwaway,
      'tree': self.tree,
    }
    if self.repo is not None:
      data['repo'] = self.repo
      data['branch'] = self.branch
    return data

  @classmethod
  def load(cls, data: dict) -> 'WorkspaceMetadata':
    required = {'isolation', 'throwaway', 'tree'}
    optional = {'repo', 'branch'}
    if not required <= data.keys() or not data.keys() <= required | optional:
      raise ValueError(f'unexpected fields: {sorted(data.keys() ^ required)}')
    repo = data.get('repo')
    branch = data.get('branch')
    tree = data['tree']
    if repo is not None and (not isinstance(repo, str) or repo == ''):
      raise ValueError('workspace repo must be a non-empty string when present')
    if branch is not None and (not isinstance(branch, str) or branch == ''):
      raise ValueError('workspace branch must be a non-empty string when present')
    if tree is not None and (not isinstance(tree, str) or tree == ''):
      raise ValueError('workspace tree must be a non-empty string when present')
    if not isinstance(data['throwaway'], bool):
      raise ValueError('workspace throwaway must be a bool')
    return cls(
      isolation=Isolation(data['isolation']),
      repo=repo,
      branch=branch,
      throwaway=data['throwaway'],
      tree=tree,
    )


def _metadata_file(name: str) -> Path:
  return workspace_dir(name) / _METADATA_FILE


def is_workspace(name: str) -> bool:
  return is_workspace_name(name) and _metadata_file(name).is_file()


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
