"""Renderer-neutral presentation blocks and mutation operations."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from bro.trails.display.config import DisplayConfig, Layout, OutputRoute


class BlockKind(StrEnum):
  MESSAGE = 'message'
  TOOL = 'tool'
  EVENT = 'event'
  METADATA = 'metadata'
  NATIVE_STEP = 'native-step'
  TRAIL_ROW = 'trail-row'
  LINEAGE_NODE = 'lineage-node'
  NOTICE = 'notice'
  STATUS = 'status'


class StyleRole(StrEnum):
  NORMAL = 'normal'
  MUTED = 'muted'
  HEADING = 'heading'
  USER = 'user'
  ASSISTANT = 'assistant'
  REASONING = 'reasoning'
  TOOL = 'tool'
  SUCCESS = 'success'
  ERROR = 'error'
  METADATA = 'metadata'
  NOTICE = 'notice'


@dataclass(frozen=True)
class BlockItem:
  text: str
  style: StyleRole = StyleRole.NORMAL
  label: str | None = None
  omitted_characters: int = 0
  markdown: bool = False

  def __post_init__(self) -> None:
    if self.omitted_characters < 0:
      raise ValueError('omitted character count must be non-negative')
    if self.label == '':
      raise ValueError('block item label must be non-empty when present')


@dataclass(frozen=True)
class PresentationBlock:
  id: str
  kind: BlockKind
  layout: Layout
  route: OutputRoute
  style: StyleRole
  label: str
  timestamp: str | None
  items: tuple[BlockItem, ...]
  depth: int = 0
  tree_last: bool = False
  pending: bool = False

  def __post_init__(self) -> None:
    if len(self.id) == 0 or len(self.label) == 0:
      raise ValueError('block id and label must not be empty')
    if self.depth < 0:
      raise ValueError('block depth must be non-negative')


@dataclass(frozen=True)
class Append:
  block: PresentationBlock


@dataclass(frozen=True)
class Update:
  block: PresentationBlock


@dataclass(frozen=True)
class Remove:
  block_id: str

  def __post_init__(self) -> None:
    if len(self.block_id) == 0:
      raise ValueError('removed block id must not be empty')


type BlockOperation = Append | Update | Remove


@dataclass(frozen=True)
class RendererCapabilities:
  retained_updates: bool
  removal: bool
  markdown: bool
  interactive: bool


@runtime_checkable
class Renderer(Protocol):
  @property
  def capabilities(self) -> RendererCapabilities: ...

  def start(self, configuration: DisplayConfig) -> None: ...

  def apply(self, operation: BlockOperation) -> None: ...

  def close(self) -> None: ...
