"""Renderer-neutral presentation blocks and mutation operations."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from bro.trails.display.config import DisplayConfig, Layout, OutputRoute


class BlockKind(StrEnum):
  MESSAGE = 'message'
  TOOL = 'tool'
  TOOL_RESULT = 'tool-result'
  EVENT = 'event'
  METADATA = 'metadata'
  CONTEXT = 'context'
  SEGMENT = 'segment'
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
  trusted_visual: bool = False
  timestamp: str | None = None

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
  calendar_date: str | None = None
  ordinal: int | None = None
  depth: int = 0
  tree_last: bool = False
  tree_ancestor_last: tuple[bool, ...] = ()
  pending: bool = False

  def __post_init__(self) -> None:
    if len(self.id) == 0 or len(self.label) == 0:
      raise ValueError('block id and label must not be empty')
    if self.ordinal is not None and self.ordinal <= 0:
      raise ValueError('block ordinal must be positive when present')
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
