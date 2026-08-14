"""Plain terminal renderers for append-only streams and retained documents."""

import os
from collections.abc import Mapping
from typing import TextIO

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
from bro.trails.display.config import ColorMode, DisplayConfig, Layout, OutputRoute

_ANSI_BY_STYLE = {
  StyleRole.NORMAL: '',
  StyleRole.MUTED: '2',
  StyleRole.HEADING: '1',
  StyleRole.USER: '34',
  StyleRole.ASSISTANT: '32',
  StyleRole.REASONING: '35',
  StyleRole.TOOL: '36',
  StyleRole.SUCCESS: '32',
  StyleRole.ERROR: '31',
  StyleRole.METADATA: '33',
  StyleRole.NOTICE: '33',
}


def color_enabled(
  mode: ColorMode, stream: TextIO | None, environment: Mapping[str, str] = os.environ
) -> bool:
  if mode is ColorMode.ALWAYS:
    return True
  if mode is ColorMode.NEVER or 'NO_COLOR' in environment:
    return False
  return stream is not None and stream.isatty()


class _TerminalFormatter:
  def __init__(self, *, color: bool, show_pending: bool):
    self._color = color
    self._show_pending = show_pending

  def block(self, block: PresentationBlock) -> str:
    if block.layout is Layout.TRAIL_LIST or block.kind is BlockKind.TRAIL_ROW:
      return self._trail_row(block)
    if block.layout is Layout.LINEAGE_TREE or block.kind is BlockKind.LINEAGE_NODE:
      return self._lineage_node(block)
    if block.layout is Layout.NATIVE_STEPS or block.kind is BlockKind.NATIVE_STEP:
      return self._native_step(block)
    return self._section(block)

  def continuation(self, previous: PresentationBlock, current: PresentationBlock) -> str:
    shared = 0
    for old_item, new_item in zip(previous.items, current.items, strict=False):
      if old_item != new_item:
        break
      shared += 1
    additions = current.items[shared:]
    if len(additions) == 0:
      return ''
    lines = [self._item_line(item, indent='  ') for item in additions]
    return '\n'.join(lines) + '\n'

  def _section(self, block: PresentationBlock) -> str:
    timestamp = f' [{block.timestamp}]' if block.timestamp is not None else ''
    pending = ' (pending)' if self._show_pending and block.pending else ''
    heading = self._styled(f'{block.label}{timestamp}{pending}', block.style)
    lines = [heading]
    lines.extend(self._item_line(item, indent='  ') for item in block.items)
    return '\n'.join(lines) + '\n\n'

  def _native_step(self, block: PresentationBlock) -> str:
    timestamp = f' {block.timestamp}' if block.timestamp is not None else ''
    parts = [self._styled(f'{block.label}{timestamp}', block.style)]
    parts.extend(self._inline_item(item) for item in block.items)
    return '  '.join(parts) + '\n'

  def _trail_row(self, block: PresentationBlock) -> str:
    parts = [self._styled(block.label, StyleRole.HEADING)]
    parts.extend(self._inline_item(item) for item in block.items)
    return '  '.join(parts) + '\n'

  def _lineage_node(self, block: PresentationBlock) -> str:
    prefix = ('    ' * block.depth) + ('└── ' if block.tree_last else '├── ')
    parts = [prefix + self._styled(block.label, StyleRole.HEADING)]
    parts.extend(self._inline_item(item) for item in block.items)
    return '  '.join(parts) + '\n'

  def _item_line(self, item: BlockItem, *, indent: str) -> str:
    marker = self._omission_marker(item)
    text = item.text if len(item.text) > 0 else '(empty)'
    prefix = f'{item.label}: ' if item.label is not None else ''
    lines = text.splitlines() or ['']
    rendered = [indent + self._styled(prefix + lines[0], item.style)]
    continuation_indent = indent + (' ' * len(prefix))
    rendered.extend(continuation_indent + self._styled(line, item.style) for line in lines[1:])
    if len(marker) > 0:
      rendered[-1] += self._styled(marker, StyleRole.MUTED)
    return '\n'.join(rendered)

  def _inline_item(self, item: BlockItem) -> str:
    text = ' '.join(item.text.split()) if len(item.text) > 0 else '(empty)'
    prefix = f'{item.label}=' if item.label is not None else ''
    return self._styled(prefix + text + self._omission_marker(item), item.style)

  @staticmethod
  def _omission_marker(item: BlockItem) -> str:
    if item.omitted_characters == 0:
      return ''
    return f'… <{item.omitted_characters} more chars>'

  def _styled(self, text: str, style: StyleRole) -> str:
    code = _ANSI_BY_STYLE[style]
    if not self._color or len(code) == 0:
      return text
    return f'\033[{code}m{text}\033[0m'


class StreamRenderer:
  """Render operations immediately without terminal cursor manipulation."""

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
    environment: Mapping[str, str] = os.environ,
  ):
    if isinstance(destinations, Mapping):
      self._destinations = dict(destinations)
    else:
      self._destinations = dict.fromkeys(OutputRoute, destinations)
    self._environment = environment
    self._configuration: DisplayConfig | None = None
    self._blocks: dict[str, PresentationBlock] = {}
    self._closed_streams: set[int] = set()
    self._closed = False

  def start(self, configuration: DisplayConfig) -> None:
    if self._configuration is not None:
      raise RuntimeError('stream renderer is already started')
    if self._closed:
      raise RuntimeError('stream renderer is closed')
    self._configuration = configuration

  @property
  def consumer_closed(self) -> bool:
    return len(self._closed_streams) > 0

  def apply(self, operation: BlockOperation) -> None:
    if self._closed:
      raise RuntimeError('stream renderer is closed')
    if self._configuration is None:
      raise RuntimeError('stream renderer is not started')
    if isinstance(operation, Append):
      block = operation.block
      if block.id in self._blocks:
        raise ValueError(f'cannot append duplicate block {block.id!r}')
      self._blocks[block.id] = block
      self._write(block.route, self._formatter(block.route).block(block))
      return
    if isinstance(operation, Update):
      block = operation.block
      previous = self._blocks.get(block.id)
      if previous is None:
        raise ValueError(f'cannot update unknown block {block.id!r}')
      self._blocks[block.id] = block
      self._write(block.route, self._formatter(block.route).continuation(previous, block))
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

  def _formatter(self, route: OutputRoute) -> _TerminalFormatter:
    stream = self._destination(route)
    assert self._configuration is not None
    return _TerminalFormatter(
      color=color_enabled(self._configuration.color, stream, self._environment),
      show_pending=False,
    )

  def _destination(self, route: OutputRoute) -> TextIO:
    try:
      return self._destinations[route]
    except KeyError as exception:
      raise ValueError(f'no stream destination configured for route {route}') from exception

  def _write(self, route: OutputRoute, text: str) -> None:
    if len(text) == 0:
      return
    stream = self._destination(route)
    if id(stream) in self._closed_streams:
      return
    try:
      stream.write(text)
      stream.flush()
    except BrokenPipeError:
      self._closed_streams.add(id(stream))


class RetainedRenderer:
  """Maintain a mutable terminal document and render it on demand."""

  capabilities = RendererCapabilities(
    retained_updates=True,
    removal=True,
    markdown=False,
    interactive=False,
  )

  def __init__(
    self,
    *,
    target: TextIO | None = None,
    environment: Mapping[str, str] = os.environ,
  ):
    self._target = target
    self._environment = environment
    self._configuration: DisplayConfig | None = None
    self._order: list[str] = []
    self._blocks: dict[str, PresentationBlock] = {}
    self._closed = False

  def start(self, configuration: DisplayConfig) -> None:
    if self._configuration is not None:
      raise RuntimeError('retained renderer is already started')
    if self._closed:
      raise RuntimeError('retained renderer is closed')
    self._configuration = configuration

  def apply(self, operation: BlockOperation) -> None:
    if self._closed:
      raise RuntimeError('retained renderer is closed')
    if self._configuration is None:
      raise RuntimeError('retained renderer is not started')
    if isinstance(operation, Append):
      block = operation.block
      if block.id in self._blocks:
        raise ValueError(f'cannot append duplicate block {block.id!r}')
      self._order.append(block.id)
      self._blocks[block.id] = block
      return
    if isinstance(operation, Update):
      block = operation.block
      if block.id not in self._blocks:
        raise ValueError(f'cannot update unknown block {block.id!r}')
      self._blocks[block.id] = block
      return
    if isinstance(operation, Remove):
      if operation.block_id not in self._blocks:
        raise ValueError(f'cannot remove unknown block {operation.block_id!r}')
      del self._blocks[operation.block_id]
      self._order.remove(operation.block_id)
      return
    raise AssertionError(f'unhandled block operation: {operation!r}')

  def document(self, route: OutputRoute | None = None) -> str:
    if self._configuration is None:
      raise RuntimeError('retained renderer is not started')
    formatter = _TerminalFormatter(
      color=color_enabled(self._configuration.color, self._target, self._environment),
      show_pending=True,
    )
    return ''.join(
      formatter.block(self._blocks[block_id])
      for block_id in self._order
      if route is None or self._blocks[block_id].route is route
    )

  @property
  def blocks(self) -> tuple[PresentationBlock, ...]:
    return tuple(self._blocks[block_id] for block_id in self._order)

  def close(self) -> None:
    self._closed = True
