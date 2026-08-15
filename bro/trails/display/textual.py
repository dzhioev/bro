"""Textual renderer and embeddable trail view for conversation displays."""

import time
from datetime import date
from typing import ClassVar

import humanize
import rich.markdown
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text
from textual.containers import Container, Vertical, VerticalScroll
from textual.content import Content
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, RichVisual
from textual.widget import Widget
from textual.widgets import Static

from bro.trails.display._reflow import Reflow
from bro.trails.display.blocks import (
  Append,
  BlockKind,
  BlockOperation,
  PresentationBlock,
  Remove,
  RendererCapabilities,
  StyleRole,
  Update,
)
from bro.trails.display.config import Appearance, DisplayConfig, Layout

_DATE_FORMAT = '%a, %b %-d, %Y'
_LOGICAL_RENDER_WIDTH = 16384


class UnpaddedCodeBlock(rich.markdown.CodeBlock):
  """A Markdown code block without decorative padding in copied text."""

  def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
    yield Syntax(str(self.text).rstrip(), self.lexer_name, theme=self.theme, word_wrap=True)


class ChatMarkdown(rich.markdown.Markdown):
  """Rich Markdown with content-hugging measurement and unpadded code blocks."""

  elements: ClassVar[dict[str, type[rich.markdown.MarkdownElement]]] = {
    **rich.markdown.Markdown.elements,
    'fence': UnpaddedCodeBlock,
    'code_block': UnpaddedCodeBlock,
  }

  def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
    return Measurement.get(console, options, Text(self.markup))


class SelectableRichVisual(RichVisual):
  """Rich visual with Textual hit-testing and logical-line copy extraction."""

  def __init__(self, widget: Widget, renderable: RenderableType):
    super().__init__(widget, renderable)
    self._logical_lines: list[str] | None = None
    self._reflow: Reflow | None = None

  def render_strips(
    self, width: int, height: int | None, style: Style, options: RenderOptions
  ) -> list[Strip]:
    strips = super().render_strips(width, height, style, options)
    if self._logical_lines is None:
      self._logical_lines = self._render_logical_lines()
    self._reflow = Reflow([strip.text for strip in strips], self._logical_lines)
    return [
      self._highlight(strip, line_index, options).apply_offsets(0, line_index)
      for line_index, strip in enumerate(strips)
    ]

  def extract_selection(self, selection: Selection) -> str | None:
    if self._reflow is None:
      return None
    return self._reflow.extract(selection.get_span)

  def _render_logical_lines(self) -> list[str]:
    app = self._widget.app
    options = app.console_options.update(highlight=False, width=_LOGICAL_RENDER_WIDTH, height=None)
    segments = app.console.render(self._renderable, options)
    return [
      ''.join(segment.text for segment in line).rstrip() for line in Segment.split_lines(segments)
    ]

  @staticmethod
  def _highlight(strip: Strip, line_index: int, options: RenderOptions) -> Strip:
    if options.selection is None or options.selection_style is None:
      return strip
    span = options.selection.get_span(line_index)
    if span is None:
      return strip
    start, end = span
    if end == -1 or end > strip.cell_length:
      end = strip.cell_length
    if start >= end:
      return strip
    before, selected, after = strip.divide([start, end, strip.cell_length])
    highlight = options.selection_style.rich_style
    highlighted = Strip(
      [
        Segment(text, highlight if style is None else style + highlight)
        for text, style, _control in selected
      ],
      selected.cell_length,
    )
    return Strip.join([before, highlighted, after])


class MessageBubble(Static):
  """Selectable conversation content with a sender-colored vertical bar."""

  DEFAULT_CSS = """
  MessageBubble {
    width: auto;
    height: auto;
    padding: 0 1;
    border: none;
    max-width: 70%;
  }
  MessageBubble.user {
    border-left: tall #2ecc71;
  }
  MessageBubble.bro {
    border-left: tall $primary;
  }
  MessageBubble.error {
    border-left: tall $error;
    color: $error;
  }
  MessageBubble.thinking {
    border-left: tall $primary 40%;
    color: $text-muted;
  }
  """

  def __init__(self, text: RenderableType, *, kind: str):
    self._visual: SelectableRichVisual | None = None
    super().__init__(classes=kind)
    self.set_content(text)

  def set_content(self, text: RenderableType) -> None:
    if isinstance(text, str):
      self._visual = None
      self.update(Content(text))
    else:
      self._visual = SelectableRichVisual(self, text)
      self.update(self._visual)

  def get_selection(self, selection: Selection) -> tuple[str, str] | None:
    if self._visual is None:
      return super().get_selection(selection)
    extracted = self._visual.extract_selection(selection)
    if extracted is None:
      return None
    return extracted, '\n'


class SystemBubble(Static):
  """Selectable muted full-width activity line."""

  DEFAULT_CSS = """
  SystemBubble {
    width: 100%;
    height: auto;
    padding: 0 2;
    margin: 0 1;
    color: $text-muted;
  }
  """

  def __init__(self, text: str):
    super().__init__(text, markup=False)

  def set_content(self, text: str) -> None:
    self.update(Content(text))


class BubbleRow(Vertical):
  """A message bubble and timestamp aligned to the conversation side."""

  DEFAULT_CSS = """
  BubbleRow {
    width: 100%;
    height: auto;
    margin: 0 1 1 1;
  }
  BubbleRow > Container {
    width: 100%;
    height: auto;
    align-horizontal: left;
  }
  BubbleRow.user > Container {
    align-horizontal: right;
  }
  BubbleRow .timestamp {
    width: auto;
    height: 1;
  }
  """

  def __init__(self, bubble: MessageBubble, *, kind: str, timestamp: str | None):
    self.bubble = bubble
    timestamp_widget = Static(Content.assemble((timestamp or '', 'dim')), classes='timestamp')
    super().__init__(Container(bubble), Container(timestamp_widget), classes=kind)


class DateSeparator(Static):
  """Centered date label inserted when conversation records cross a day."""

  DEFAULT_CSS = """
  DateSeparator {
    height: 1;
    content-align: center middle;
    color: $text-muted;
    margin: 1 0;
  }
  """

  def __init__(self, calendar_date: str):
    super().__init__(date.fromisoformat(calendar_date).strftime(_DATE_FORMAT))


def _typing_status(activity: str, phase_seconds: float) -> str:
  if activity == 'thinking':
    return f'Thinking for {humanize.naturaldelta(phase_seconds)}'
  if activity.startswith('calling ') and activity.endswith(' tools'):
    return activity.capitalize()
  if activity.startswith('calling '):
    name = activity.removeprefix('calling ')
    if phase_seconds > 1:
      return f'Calling {name} for {humanize.naturaldelta(phase_seconds)}'
    return f'Calling {name}()'
  raise ValueError(f'unknown conversation activity: {activity!r}')


class TypingIndicator(Container):
  """Animated transient activity bubble driven by a status presentation block."""

  DEFAULT_CSS = """
  TypingIndicator {
    height: auto;
    layout: horizontal;
    align-horizontal: left;
    margin: 0 1;
  }
  TypingIndicator > Static {
    padding: 0 1;
    color: $text-muted;
  }
  """

  def __init__(self, activity: str):
    self._activity = activity
    self._phase_since = time.monotonic()
    self._animation_step = 0
    self._label = Static(self._status())
    super().__init__(self._label)

  def on_mount(self) -> None:
    self.set_interval(0.4, self.tick)

  def set_activity(self, activity: str) -> None:
    if activity != self._activity:
      self._activity = activity
      self._phase_since = time.monotonic()
    self._label.update(self._status())

  def tick(self) -> None:
    self._animation_step = (self._animation_step + 1) % 4
    self._label.update(self._status())

  def _status(self) -> str:
    status = _typing_status(self._activity, time.monotonic() - self._phase_since)
    return status + '.' * self._animation_step


class TrailView(VerticalScroll):
  """Embeddable retained view of conversation presentation blocks."""

  def __init__(self, *children: Widget, **kwargs):
    super().__init__(*children, **kwargs)
    self._last_date: str | None = None
    self._status_widgets: list[Widget] = []

  def mount_record(self, widget: Widget, *, calendar_date: str | None, status: bool) -> None:
    if calendar_date is not None and calendar_date != self._last_date:
      self.mount(DateSeparator(calendar_date), before=self._first_status())
      self._last_date = calendar_date
    self.mount(widget, before=None if status else self._first_status())
    if status:
      self._status_widgets.append(widget)
    self._scroll_to_end()

  def forget_status(self, widget: Widget) -> None:
    self._status_widgets.remove(widget)

  def _first_status(self) -> Widget | None:
    return self._status_widgets[0] if len(self._status_widgets) > 0 else None

  def _scroll_to_end(self) -> None:
    self.call_after_refresh(lambda: self.scroll_end(animate=False))


class TextualRenderer:
  """Retained interactive renderer targeting an embedded :class:`TrailView`."""

  capabilities = RendererCapabilities(
    retained_updates=True,
    removal=True,
    markdown=True,
    interactive=True,
  )

  def __init__(self, view: TrailView):
    self._view = view
    self._configuration: DisplayConfig | None = None
    self._widgets: dict[str, Widget] = {}
    self._blocks: dict[str, PresentationBlock] = {}
    self._closed = False

  def start(self, configuration: DisplayConfig) -> None:
    if self._configuration is not None:
      raise RuntimeError('Textual renderer is already started')
    if self._closed:
      raise RuntimeError('Textual renderer is closed')
    if (
      configuration.layout is not Layout.CONVERSATION
      or configuration.appearance is not Appearance.CHAT
    ):
      raise ValueError('Textual trail view requires a chat conversation configuration')
    self._configuration = configuration

  def apply(self, operation: BlockOperation) -> None:
    if self._closed:
      raise RuntimeError('Textual renderer is closed')
    if self._configuration is None:
      raise RuntimeError('Textual renderer is not started')
    if isinstance(operation, Append):
      block = operation.block
      if block.id in self._widgets:
        raise ValueError(f'cannot append duplicate block {block.id!r}')
      widget = self._widget_for(block)
      self._widgets[block.id] = widget
      self._blocks[block.id] = block
      self._view.mount_record(
        widget,
        calendar_date=block.calendar_date,
        status=block.kind is BlockKind.STATUS,
      )
      return
    if isinstance(operation, Update):
      block = operation.block
      widget = self._widgets.get(block.id)
      if widget is None:
        raise ValueError(f'cannot update unknown block {block.id!r}')
      self._update_widget(widget, block)
      self._blocks[block.id] = block
      return
    if isinstance(operation, Remove):
      widget = self._widgets.pop(operation.block_id, None)
      if widget is None:
        raise ValueError(f'cannot remove unknown block {operation.block_id!r}')
      block = self._blocks.pop(operation.block_id)
      if block.kind is BlockKind.STATUS:
        self._view.forget_status(widget)
      widget.remove()
      return
    raise AssertionError(f'unhandled block operation: {operation!r}')

  def close(self) -> None:
    self._closed = True

  def _widget_for(self, block: PresentationBlock) -> Widget:
    if block.kind is BlockKind.MESSAGE:
      return self._message_row(block)
    if block.kind is BlockKind.NOTICE:
      if block.label.endswith('interruption'):
        return SystemBubble(block.items[0].text)
      return self._message_row(block, kind='bro')
    if block.kind in {BlockKind.TOOL, BlockKind.TOOL_RESULT, BlockKind.EVENT}:
      return SystemBubble(self._system_text(block))
    if block.kind is BlockKind.STATUS:
      return TypingIndicator(block.items[0].text)
    return SystemBubble(self._system_text(block))

  def _message_row(self, block: PresentationBlock, *, kind: str | None = None) -> BubbleRow:
    effective_kind = kind or self._message_kind(block.style)
    bubble = MessageBubble(self._message_content(block), kind=effective_kind)
    return BubbleRow(bubble, kind=effective_kind, timestamp=block.timestamp)

  @staticmethod
  def _message_kind(style: StyleRole) -> str:
    if style is StyleRole.USER:
      return 'user'
    if style is StyleRole.REASONING:
      return 'thinking'
    if style is StyleRole.ERROR:
      return 'error'
    return 'bro'

  @staticmethod
  def _message_content(block: PresentationBlock) -> RenderableType:
    text = '\n\n'.join(item.text for item in block.items)
    if any(item.trusted_visual for item in block.items):
      return Text.from_ansi(text)
    if any(item.markdown for item in block.items):
      return ChatMarkdown(text)
    return Text(text) if '\n' in text else text

  @staticmethod
  def _system_text(block: PresentationBlock) -> str:
    if block.kind is BlockKind.TOOL:
      arguments = block.items[0].text
      lines = [f'→ {block.label}({arguments})']
      if len(block.items) > 1:
        lines.append(f'← {block.label}')
      return '\n'.join(lines)
    if block.kind is BlockKind.TOOL_RESULT:
      name = block.label.partition(' · ')[2] or block.label
      return f'← {name}'
    return '\n'.join(item.text for item in block.items)

  def _update_widget(self, widget: Widget, block: PresentationBlock) -> None:
    if isinstance(widget, BubbleRow):
      widget.bubble.set_content(self._message_content(block))
    elif isinstance(widget, SystemBubble):
      widget.set_content(self._system_text(block))
    elif isinstance(widget, TypingIndicator):
      widget.set_activity(block.items[0].text)
    else:
      raise AssertionError(f'unhandled Textual trail widget: {widget!r}')
