"""A workspace's recorded identity, isolation, and typed attachments."""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Optional

from bro.workspace.paths import workspace_dir
from ride.attachments import Attachment

_METADATA_FILE = 'workspace.json'


class WorkspaceNotFound(ValueError):
  pass


class UnrecognizedWorkspaceRecord(ValueError):
  pass


def unrecognized_workspace_record(name: str, error: object) -> UnrecognizedWorkspaceRecord:
  return UnrecognizedWorkspaceRecord(
    f'workspace {name!r} has an unrecognised record: {error}; remove it with `ride clean --force {name}`'
  )


class Isolation(StrEnum):
  BOXED = 'boxed'
  UNBOXED = 'unboxed'


@dataclass(frozen=True)
class WorkspaceMetadata:
  isolation: Isolation
  attachments: tuple[Attachment, ...] = ()
  throwaway: bool = False
  tree: Optional[str] = None

  def __post_init__(self) -> None:
    if len(self.attachments) > 1:
      raise ValueError('a workspace supports at most one attachment')
    if self.tree is not None:
      if self.isolation is not Isolation.UNBOXED:
        raise ValueError('an external workspace tree requires unboxed isolation')
      if self.attachments:
        raise ValueError('an external workspace tree requires a detached workspace')
      if not Path(self.tree).is_absolute():
        raise ValueError('workspace tree must be an absolute path')

  def dump(self) -> dict:
    data: dict = {
      'isolation': self.isolation.value,
      'throwaway': self.throwaway,
      'tree': self.tree,
    }
    if self.attachments:
      data['attachments'] = [attachment.dump() for attachment in self.attachments]
    return data

  @classmethod
  def load(cls, data: dict) -> 'WorkspaceMetadata':
    required = {'isolation', 'throwaway', 'tree'}
    optional = {'attachments'}
    if missing := required - data.keys():
      raise ValueError(f'missing fields: {sorted(missing)}')
    if unexpected := data.keys() - (required | optional):
      raise ValueError(f'unexpected fields: {sorted(unexpected)}')
    attachments = data.get('attachments', [])
    if 'attachments' in data and (not isinstance(attachments, list) or not attachments):
      raise ValueError('workspace attachments must be a non-empty list when present')
    tree = data['tree']
    if tree is not None and (not isinstance(tree, str) or tree == ''):
      raise ValueError('workspace tree must be a non-empty string when present')
    if not isinstance(data['throwaway'], bool):
      raise ValueError('workspace throwaway must be a bool')
    return cls(
      isolation=Isolation(data['isolation']),
      attachments=tuple(Attachment.load(attachment) for attachment in attachments),
      throwaway=data['throwaway'],
      tree=tree,
    )


def _metadata_file(name: str) -> Path:
  return workspace_dir(name) / _METADATA_FILE


def read_metadata(name: str) -> WorkspaceMetadata:
  file = _metadata_file(name)
  try:
    data = json.loads(file.read_text())
  except FileNotFoundError as exception:
    workspace = workspace_dir(name)
    if workspace.is_dir():
      raise unrecognized_workspace_record(name, exception) from exception
    raise WorkspaceNotFound(f'workspace not found: {name}') from exception
  except (OSError, UnicodeError, json.JSONDecodeError) as exception:
    raise unrecognized_workspace_record(name, exception) from exception
  try:
    if not isinstance(data, dict):
      raise ValueError('workspace record must be an object')
    return WorkspaceMetadata.load(data)
  except Exception as exception:
    raise unrecognized_workspace_record(name, exception) from exception


def write_metadata(name: str, metadata: WorkspaceMetadata) -> None:
  file = _metadata_file(name)
  file.parent.mkdir(parents=True, exist_ok=True)
  file.write_text(json.dumps(metadata.dump(), indent=2))
