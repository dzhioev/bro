"""Lazy Rich-backed panel rendering for explicit terminal opt-in."""

from collections.abc import Callable, Mapping
from typing import Any, TextIO

from bro.trails.display.blocks import (
  Append,
  BlockItem,
  BlockKind,
  BlockOperation,
  PresentationBlock,
  Remove,
  RendererCapabilities,
  StyleRole,
  Update,
)
from bro.trails.display.config import DisplayConfig, OutputRoute
from bro.trails.display.records import RecordKind

_BORDER_STYLE = {
  StyleRole.NORMAL: 'white',
  StyleRole.MUTED: 'dim',
  StyleRole.HEADING: 'bright_white',
  StyleRole.USER: 'blue',
  StyleRole.ASSISTANT: 'bright_blue',
  StyleRole.REASONING: 'magenta',
  StyleRole.TOOL: 'cyan',
  StyleRole.SUCCESS: 'green',
  StyleRole.ERROR: 'red',
  StyleRole.METADATA: 'yellow',
  StyleRole.NOTICE: 'yellow',
}


class RichPanelRenderer:
  """Render append-only block operations as Rich panels without leaking Rich types."""

  capabilities = RendererCapabilities(
    retained_updates=False,
    removal=False,
    markdown=False,
    interactive=False,
  )

  def __init__(
    self,
    destinations: TextIO | Mapping[OutputRoute, TextIO],
    *,
    console_factory: Callable[[TextIO], Any] | None = None,
  ):
    if isinstance(destinations, Mapping):
      self._destinations = dict(destinations)
    else:
      self._destinations = dict.fromkeys(OutputRoute, destinations)
    self._console_factory = console_factory
    self._configuration: DisplayConfig | None = None
    self._blocks: dict[str, PresentationBlock] = {}
    self._consoles: dict[int, Any] = {}
    self._closed_streams: set[int] = set()
    self._closed = False

  @property
  def consumer_closed(self) -> bool:
    return len(self._closed_streams) > 0

  def start(self, configuration: DisplayConfig) -> None:
    if self._configuration is not None:
      raise RuntimeError('Rich panel renderer is already started')
    if self._closed:
      raise RuntimeError('Rich panel renderer is closed')
    self._configuration = configuration

  def apply(self, operation: BlockOperation) -> None:
    if self._closed:
      raise RuntimeError('Rich panel renderer is closed')
    if self._configuration is None:
      raise RuntimeError('Rich panel renderer is not started')
    if isinstance(operation, Append):
      block = operation.block
      if block.id in self._blocks:
        raise ValueError(f'cannot append duplicate block {block.id!r}')
      self._blocks[block.id] = block
      self._render(block, block.items)
      return
    if isinstance(operation, Update):
      block = operation.block
      previous = self._blocks.get(block.id)
      if previous is None:
        raise ValueError(f'cannot update unknown block {block.id!r}')
      self._blocks[block.id] = block
      shared = 0
      for old_item, new_item in zip(previous.items, block.items, strict=False):
        if old_item != new_item:
          break
        shared += 1
      additions = block.items[shared:]
      if len(additions) > 0:
        self._render(block, additions, continuation=True)
      return
    if isinstance(operation, Remove):
      if operation.block_id not in self._blocks:
        raise ValueError(f'cannot remove unknown block {operation.block_id!r}')
      del self._blocks[operation.block_id]
      return
    raise AssertionError(f'unhandled block operation: {operation!r}')

  def close(self) -> None:
    if self._closed:
      return
    seen_streams: set[int] = set()
    for stream in self._destinations.values():
      identity = id(stream)
      if identity in seen_streams or identity in self._closed_streams:
        continue
      seen_streams.add(identity)
      try:
        stream.flush()
      except BrokenPipeError:
        self._closed_streams.add(identity)
    self._closed = True

  def _render(
    self,
    block: PresentationBlock,
    items: tuple[BlockItem, ...],
    *,
    continuation: bool = False,
  ) -> None:
    stream = self._destination(block.route)
    if id(stream) in self._closed_streams:
      return
    if block.route is OutputRoute.REPLY:
      self._write_reply(stream, items)
      return
    label = block.label
    style = block.style
    if continuation and block.kind is BlockKind.TOOL:
      label = f'{self._label(RecordKind.TOOL_RESULT)} · {block.label}'
      style = items[-1].style
    elif block.kind is BlockKind.TOOL:
      label = f'{self._label(RecordKind.TOOL_CALL)} · {block.label}'
      items = items[:1]
    title = self._title(label, block.timestamp)
    try:
      from rich.panel import Panel
      from rich.text import Text

      body = Text(self._body(items))
      self._console(stream).print(
        Panel(body, title=Text(title), border_style=_BORDER_STYLE[style], title_align='left')
      )
    except BrokenPipeError:
      self._closed_streams.add(id(stream))

  def _body(self, items: tuple[BlockItem, ...]) -> str:
    lines = []
    for item in items:
      prefix = f'{item.label}: ' if item.label is not None else ''
      lines.append(prefix + item.text)
      if item.omitted_characters > 0:
        lines.append(f'... <{item.omitted_characters} more chars>')
    return '\n'.join(lines)

  def _label(self, kind: RecordKind) -> str:
    assert self._configuration is not None
    return self._configuration.labels.for_kind(kind)

  def _title(self, label: str, timestamp: str | None) -> str:
    assert self._configuration is not None
    parts = []
    if timestamp is not None:
      parts.append(f'[{timestamp}]')
    if len(self._configuration.context_label) > 0:
      parts.append(self._configuration.context_label)
    parts.append(label)
    return ' · '.join(parts)

  def _console(self, stream: TextIO) -> Any:
    identity = id(stream)
    console = self._consoles.get(identity)
    if console is not None:
      return console
    if self._console_factory is None:
      from rich.console import Console

      console = Console(file=stream, highlight=False, force_terminal=True)
    else:
      console = self._console_factory(stream)
    self._consoles[identity] = console
    return console

  def _destination(self, route: OutputRoute) -> TextIO:
    try:
      return self._destinations[route]
    except KeyError as exception:
      raise ValueError(f'no stream destination configured for route {route}') from exception

  def _write_reply(self, stream: TextIO, items: tuple[BlockItem, ...]) -> None:
    text = ''.join(
      item.text
      + (f'\n... <{item.omitted_characters} more chars>' if item.omitted_characters > 0 else '')
      + '\n'
      for item in items
    )
    try:
      stream.write(text)
      stream.flush()
    except BrokenPipeError:
      self._closed_streams.add(id(stream))
