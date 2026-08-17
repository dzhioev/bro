"""Plain terminal renderers for append-only streams and retained documents."""

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
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
from bro.trails.display.config import Appearance, ColorMode, DisplayConfig, OutputRoute
from bro.trails.display.records import RecordKind

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
  def __init__(self, configuration: DisplayConfig, *, color: bool):
    self._configuration = configuration
    self._color = color
    self._rewind_turns: set[int] = set()
    self._context_open = False
    self._chat_date: str | None = None

  def block(self, block: PresentationBlock) -> str:
    if self._configuration.appearance is Appearance.PLAIN_LOG:
      return self._plain_log(block)
    if self._configuration.appearance is Appearance.CHAT:
      return self._chat_date_separator(block) + self._chat(block)
    return self._rewind(block)

  def continuation(self, previous: PresentationBlock, current: PresentationBlock) -> str:
    shared = 0
    for old_item, new_item in zip(previous.items, current.items, strict=False):
      if old_item != new_item:
        break
      shared += 1
    additions = current.items[shared:]
    if len(additions) == 0:
      return ''
    if self._configuration.appearance is Appearance.PLAIN_LOG:
      if current.kind is BlockKind.TOOL:
        return ''.join(
          self._plain_section(
            f'{self._configuration.labels.for_kind(RecordKind.TOOL_RESULT)}: {current.label}',
            item.timestamp,
            (item,),
            show_labels=False,
            style=item.style,
          )
          for item in additions
        )
      return self._plain_section(current.label, current.timestamp, additions)
    if self._configuration.appearance is Appearance.CHAT:
      if current.kind is BlockKind.TOOL:
        return ''.join(self._chat_tool_result(current, item) for item in additions)
      return ''.join(self._chat_item(current, item) for item in additions)
    if current.kind is BlockKind.TOOL:
      return ''.join(self._rewind_late_result(item) for item in additions)
    continuation = replace(current, items=additions, ordinal=None)
    return self._rewind_conversation(continuation)

  def finish(self) -> str:
    if not self._context_open:
      return ''
    self._context_open = False
    return self._styled('─' * 78, StyleRole.MUTED) + '\n'

  def _plain_log(self, block: PresentationBlock) -> str:
    if block.route is OutputRoute.REPLY:
      return ''.join(item.text + self._plain_omission(item) + '\n' for item in block.items)
    if block.kind is BlockKind.TOOL_RESULT:
      name = block.label.partition(' · ')[2] or block.label
      return self._plain_section(
        f'{self._configuration.labels.for_kind(RecordKind.TOOL_RESULT)}: {name}',
        block.timestamp,
        block.items,
        show_labels=False,
        style=block.style,
      )
    if block.kind is BlockKind.TOOL:
      output = self._plain_section(
        f'{self._configuration.labels.for_kind(RecordKind.TOOL_CALL)}: {block.label}',
        block.timestamp,
        block.items[:1],
        show_labels=False,
        style=StyleRole.TOOL,
      )
      if len(block.items) > 1:
        output += ''.join(
          self._plain_section(
            f'{self._configuration.labels.for_kind(RecordKind.TOOL_RESULT)}: {block.label}',
            item.timestamp,
            (item,),
            show_labels=False,
            style=item.style,
          )
          for item in block.items[1:]
        )
      return output
    return self._plain_section(block.label, block.timestamp, block.items, style=block.style)

  def _plain_section(
    self,
    label: str,
    timestamp: str | None,
    items: tuple[BlockItem, ...],
    *,
    show_labels: bool = True,
    style: StyleRole = StyleRole.NORMAL,
  ) -> str:
    heading = []
    if timestamp is not None:
      heading.append(f'[{timestamp}]')
    if len(self._configuration.context_label) > 0:
      heading.append(self._configuration.context_label)
    heading.append(self._styled(label, style))
    lines = [' '.join(heading)]
    for item in items:
      prefix = f'{item.label}: ' if show_labels and item.label is not None else ''
      text = prefix + item.text
      lines.extend(f'  {self._styled(line, item.style)}' for line in text.splitlines())
      if item.omitted_characters > 0:
        lines.append(f'  ... <{item.omitted_characters} more chars>')
    lines.append('')
    return '\n'.join(lines) + '\n'

  @staticmethod
  def _plain_omission(item: BlockItem) -> str:
    if item.omitted_characters == 0:
      return ''
    return f'\n... <{item.omitted_characters} more chars>'

  def _chat_date_separator(self, block: PresentationBlock) -> str:
    if block.calendar_date is None or block.calendar_date == self._chat_date:
      return ''
    self._chat_date = block.calendar_date
    rendered_date = date.fromisoformat(block.calendar_date).strftime('%a, %b %-d, %Y')
    return f'--- {rendered_date} ---\n'

  def _chat(self, block: PresentationBlock) -> str:
    if block.kind is BlockKind.TOOL_RESULT:
      name = block.label.partition(' · ')[2] or block.label
      item = block.items[0]
      timestamp = (
        f'[{item.timestamp or block.timestamp}] ' if item.timestamp or block.timestamp else ''
      )
      speaker = self._configuration.context_label
      prefix = f'{speaker} ' if len(speaker) > 0 else ''
      return f'{timestamp}{prefix}← {name}\n'
    if block.kind is BlockKind.TOOL:
      output = self._chat_tool_call(block)
      output += ''.join(self._chat_tool_result(block, item) for item in block.items[1:])
      return output
    if block.kind is BlockKind.NOTICE:
      item = block.items[0]
      timestamp = f'[{block.timestamp}] ' if block.timestamp is not None else ''
      if block.label.endswith('interruption'):
        return f'{timestamp}{item.text}\n'
      speaker = self._configuration.context_label or block.label
      return f'{timestamp}{speaker}:\n{item.text}{self._chat_omission(item)}\n'
    return ''.join(self._chat_item(block, item) for item in block.items)

  def _chat_item(self, block: PresentationBlock, item: BlockItem) -> str:
    timestamp = f'[{block.timestamp}] ' if block.timestamp is not None else ''
    speaker = self._configuration.context_label or block.label
    omission = self._chat_omission(item)
    if block.style is StyleRole.REASONING:
      one_line_text = ' '.join(item.text.split())
      return f'{timestamp}{speaker} · {block.label}: {one_line_text}{omission}\n'
    return f'{timestamp}{speaker}: {item.text}{omission}\n'

  def _chat_tool_call(self, block: PresentationBlock) -> str:
    timestamp = f'[{block.timestamp}] ' if block.timestamp is not None else ''
    speaker = self._configuration.context_label
    prefix = f'{speaker} ' if len(speaker) > 0 else ''
    arguments = block.items[0].text
    return f'{timestamp}{prefix}→ {block.label}({arguments})\n'

  def _chat_tool_result(self, block: PresentationBlock, item: BlockItem) -> str:
    timestamp = f'[{item.timestamp}] ' if item.timestamp is not None else ''
    speaker = self._configuration.context_label
    prefix = f'{speaker} ' if len(speaker) > 0 else ''
    return f'{timestamp}{prefix}← {block.label}\n'

  @staticmethod
  def _chat_omission(item: BlockItem) -> str:
    if item.omitted_characters == 0:
      return ''
    return f'… <{item.omitted_characters} more chars>'

  def _rewind(self, block: PresentationBlock) -> str:
    prefix = ''
    if self._context_open and block.kind is not BlockKind.CONTEXT:
      prefix = self.finish()
    if block.kind is BlockKind.METADATA:
      return prefix + self._rewind_metadata(block)
    if block.kind is BlockKind.CONTEXT:
      return prefix + self._rewind_context(block)
    if block.kind is BlockKind.SEGMENT:
      return prefix + self._rewind_segment(block)
    if block.kind is BlockKind.NATIVE_STEP:
      return prefix + self._rewind_native_step(block)
    if block.kind is BlockKind.TRAIL_ROW:
      return prefix + self._rewind_trail_row(block)
    if block.kind is BlockKind.LINEAGE_NODE:
      return prefix + self._rewind_lineage_node(block)
    if block.kind is BlockKind.TOOL_RESULT:
      return (
        prefix
        + self._rewind_turn_heading(block)
        + ''.join(self._rewind_late_result(item) for item in block.items)
      )
    return prefix + self._rewind_conversation(block)

  def _rewind_metadata(self, block: PresentationBlock) -> str:
    labelled = [item for item in block.items if item.label is not None]
    if len(labelled) == 0:
      return ''
    width = max(len(item.label or '') for item in labelled)
    lines = []
    for index, item in enumerate(labelled):
      label_style = StyleRole.HEADING if index == 0 else StyleRole.MUTED
      label = self._styled(f'{item.label:<{width}}', label_style)
      lines.append(f'{label} {item.text}{self._rewind_omission(item)}')
    lines.append(self._styled('─' * 78, StyleRole.MUTED))
    return '\n'.join(lines) + '\n'

  def _rewind_context(self, block: PresentationBlock) -> str:
    lines = []
    if not self._context_open:
      heading = self._configuration.labels.for_kind(RecordKind.LAUNCH_CONTEXT)
      lines.append(self._styled(heading, StyleRole.HEADING))
      self._context_open = True
    lines.append(self._styled(f'▸ {block.label}', StyleRole.METADATA))
    for item in block.items:
      if item.label is None:
        lines.extend(self._styled(f'  {line}', StyleRole.MUTED) for line in item.text.splitlines())
      else:
        lines.append(self._styled(f'  {item.label} {item.text}', StyleRole.MUTED))
    return '\n'.join(lines) + '\n'

  def _rewind_segment(self, block: PresentationBlock) -> str:
    fields = {item.label: item.text for item in block.items}
    segment = fields.get('segment', '?')[:8]
    timestamp = block.timestamp or '-'
    text = f'── resumed as trail {fields.get("trail", "?")} (segment {segment}) · {timestamp} ──'
    return '\n' + self._styled(text, StyleRole.MUTED) + '\n'

  def _rewind_conversation(self, block: PresentationBlock) -> str:
    output = self._rewind_turn_heading(block)
    if block.kind is BlockKind.TOOL:
      output += self._rewind_tool_call(block.label, block.items[0])
      for item in block.items[1:]:
        rendered = item.text if len(item.text) > 0 else '(empty)'
        output += ''.join(
          self._styled(f'    {line}', StyleRole.MUTED) + '\n' for line in rendered.splitlines()
        )
        output += self._rewind_omitted_line(item)
      return output
    for item in block.items:
      if block.style is StyleRole.REASONING:
        output += f'  {self._styled("[thinking]", StyleRole.MUTED)}\n'
        output += ''.join(
          self._styled(f'    {line}', StyleRole.MUTED) + '\n' for line in item.text.splitlines()
        )
      else:
        rendered = item.text if len(item.text) > 0 else '(no detail)'
        output += ''.join(f'  {line}\n' for line in rendered.splitlines())
      output += self._rewind_omitted_line(item)
    return output

  def _rewind_tool_call(self, label: str, item: BlockItem) -> str:
    if '\n' in item.text:
      output = f'  {self._styled(f"→ {label}", StyleRole.TOOL)}\n'
      output += ''.join(
        (self._styled(f'      {line}', StyleRole.TOOL) if len(line) > 0 else '') + '\n'
        for line in item.text.rstrip('\n').split('\n')
      )
    else:
      arguments = f' {item.text}' if len(item.text) > 0 else ''
      output = f'  {self._styled(f"→ {label}{arguments}", StyleRole.TOOL)}\n'
    return output + self._rewind_omitted_line(item)

  def _rewind_omitted_line(self, item: BlockItem) -> str:
    suffix = self._rewind_omission(item)
    return '' if len(suffix) == 0 else self._styled(f'  {suffix}\n', StyleRole.MUTED)

  def _rewind_turn_heading(self, block: PresentationBlock) -> str:
    if block.ordinal is None or block.ordinal in self._rewind_turns:
      return ''
    self._rewind_turns.add(block.ordinal)
    if block.style is StyleRole.USER:
      role_style = StyleRole.USER
      role = block.label
    elif block.style is StyleRole.ERROR:
      role_style = StyleRole.ERROR
      role = self._configuration.labels.for_kind(RecordKind.ERROR)
    else:
      role_style = StyleRole.ASSISTANT
      kind = RecordKind.TOOL_CALL if block.kind is BlockKind.TOOL else RecordKind.ASSISTANT
      role = self._configuration.labels.for_kind(kind)
    timestamp = block.timestamp or '-'
    return (
      f'\n{self._styled(f"#{block.ordinal}", StyleRole.HEADING)} '
      f'{self._styled(role, role_style)} {self._styled(timestamp, StyleRole.MUTED)}\n'
    )

  def _rewind_late_result(self, item: BlockItem) -> str:
    rendered = item.text if len(item.text) > 0 else '(empty)'
    lines = rendered.splitlines()
    if len(lines) == 0:
      lines = ['(empty)']
    output = [f'  {self._styled(f"← {lines[0]}", StyleRole.MUTED)}']
    output.extend(self._styled(f'  {line}', StyleRole.MUTED) for line in lines[1:])
    return '\n'.join(output) + '\n'

  def _rewind_native_step(self, block: PresentationBlock) -> str:
    step_id = block.label.removeprefix('step ')
    body = block.items[0]
    attributes = {item.label: item for item in block.items[1:] if item.label is not None}
    turn = attributes.pop('turn_index', None)
    turn_text = f't{turn.text} ' if turn is not None else ''
    step_kind = body.label or '?'
    timestamp = block.timestamp or '-'
    prefix = (
      f'{self._styled(step_id, StyleRole.METADATA)}  '
      f'{self._styled(timestamp, StyleRole.MUTED)}  '
      f'{self._styled(f"{turn_text}{step_kind:<14}", StyleRole.METADATA)}'
    )
    text = body.text.replace('\n', ' ').replace('\r', ' ')
    summary = text + self._rewind_omission(body)
    parts = [prefix]
    if len(summary) > 0:
      parts.append(self._styled(summary, body.style))
    if len(attributes) > 0:
      extras = []
      for label, item in attributes.items():
        shown_label = 'args' if label == 'arguments' else label
        text = item.text.replace('\n', ' ').replace('\r', ' ')
        extras.append(
          f'{self._styled(shown_label, StyleRole.TOOL)}={text}{self._rewind_omission(item)}'
        )
      parts.append(f'[{" ".join(extras)}]')
    return '  '.join(parts) + '\n'

  def _rewind_trail_row(self, block: PresentationBlock) -> str:
    items = {item.label: item for item in block.items}
    timestamp = block.timestamp or '-'
    harness = items['harness']
    owner = items['owner']
    model = items['model']
    status = items['status']
    text = (
      f'{self._styled(block.label, StyleRole.METADATA)}  '
      f'{self._styled(timestamp, StyleRole.MUTED)}  '
      f'{self._styled(f"{harness.text:<6}", harness.style)}  '
      f'{self._styled(f"{owner.text:<10}", owner.style)}  '
      f'{self._styled(f"{model.text:<10}", model.style)}  '
      f'{self._styled(status.text, status.style)}'
    )
    forked_from = items.get('fork of')
    if forked_from is not None:
      text += f'  {self._styled(f"fork-of {forked_from.text}", StyleRole.MUTED)}'
    subject = items.get('subject')
    if subject is not None:
      text += f'  {self._styled(subject.text + self._rewind_omission(subject), StyleRole.MUTED)}'
    return text + '\n'

  def _rewind_lineage_node(self, block: PresentationBlock) -> str:
    if len(block.tree_ancestor_last) > 0:
      prefix = ''.join('    ' if is_last else '│   ' for is_last in block.tree_ancestor_last)
    else:
      prefix = '    ' * block.depth
    connector = '└── ' if block.tree_last else '├── '
    items = {item.label: item for item in block.items if item.label is not None}
    owner = items.get('owner')
    model = items.get('model')
    owner_text = owner.text if owner is not None else '?'
    model_text = model.text if model is not None else '?'
    text = (
      f'{prefix}{connector}{self._styled(block.label, StyleRole.METADATA)}  '
      f'{self._styled(owner_text, StyleRole.TOOL)}/'
      f'{self._styled(model_text, StyleRole.MUTED)}'
    )
    step = items.get('step')
    if step is not None:
      text += f' {self._styled(f"@step {step.text}", StyleRole.MUTED)}'
    if any(item.text == 'here' for item in block.items):
      text += f' {self._styled("<-- here", StyleRole.HEADING)}'
    return text + '\n'

  @staticmethod
  def _rewind_omission(item: BlockItem) -> str:
    if item.omitted_characters == 0:
      return ''
    return f'... <{item.omitted_characters} more chars>'

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
    self._formatters: dict[int, _TerminalFormatter] = {}
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
    for identity, formatter in self._formatters.items():
      stream = next(stream for stream in self._destinations.values() if id(stream) == identity)
      self._write_to_stream(stream, formatter.finish())
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
    identity = id(stream)
    formatter = self._formatters.get(identity)
    if formatter is not None:
      return formatter
    assert self._configuration is not None
    formatter = _TerminalFormatter(
      self._configuration,
      color=color_enabled(self._configuration.color, stream, self._environment),
    )
    self._formatters[identity] = formatter
    return formatter

  def _destination(self, route: OutputRoute) -> TextIO:
    try:
      return self._destinations[route]
    except KeyError as exception:
      raise ValueError(f'no stream destination configured for route {route}') from exception

  def _write(self, route: OutputRoute, text: str) -> None:
    self._write_to_stream(self._destination(route), text)

  def _write_to_stream(self, stream: TextIO, text: str) -> None:
    if len(text) == 0 or id(stream) in self._closed_streams:
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
      self._configuration,
      color=color_enabled(self._configuration.color, self._target, self._environment),
    )
    document = ''.join(
      formatter.block(self._blocks[block_id])
      for block_id in self._order
      if route is None or self._blocks[block_id].route is route
    )
    return document + formatter.finish()

  @property
  def blocks(self) -> tuple[PresentationBlock, ...]:
    return tuple(self._blocks[block_id] for block_id in self._order)

  def close(self) -> None:
    self._closed = True
