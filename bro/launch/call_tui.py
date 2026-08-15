"""IM-style chat TUI for `call`. Entry point: `ChatApp(bro, initial).run()`."""

import asyncio
import contextlib
import time
from datetime import date, datetime
from typing import ClassVar, Optional, assert_never

import humanize
import rich.markdown
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, RichVisual
from textual.widget import Widget
from textual.widgets import Static, TextArea
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from bro.launch._reflow import Reflow
from bro.launch._trace_format import format_tool_call
from bro.launch.call import DATE_FORMAT, INTERRUPTED_NOTICE
from bro.launch.resume import HistoryMessage
from bro.llm.mcp import canonical_name
from bro.llm.observer import (
  InterimAssistantTextEvent,
  ObservedEvent,
  Observer,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
  TurnCompletedEvent,
  TurnFailedEvent,
  TurnRefusedEvent,
  TurnStartedEvent,
)
from bro.show import format_card
from bros.bro import Bro

# the message field states: it takes text only between turns, so an interrupt is
# the one way to get the input back while the bro is working.
_IDLE_PLACEHOLDER = 'message…'
_BUSY_PLACEHOLDER = 'esc to interrupt…'


class UnpaddedCodeBlock(rich.markdown.CodeBlock):
  """`rich.markdown.CodeBlock` without its decorative padding — the pad column would
  put an invented leading space (and blank pad lines) into every copied code line."""

  def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
    yield Syntax(str(self.text).rstrip(), self.lexer_name, theme=self.theme, word_wrap=True)


class ChatMarkdown(rich.markdown.Markdown):
  """rich Markdown with content-hugging width measurement and unpadded code blocks.

  rich's Markdown reports no measurement of its own, so a `width: auto` widget
  gives it the full available width and every bubble stretches to its
  max-width. measure the raw text instead — an upper bound, since markdown
  syntax and link targets render narrower than they read — so short messages
  keep tight bubbles.
  """

  elements: ClassVar[dict[str, type[rich.markdown.MarkdownElement]]] = {
    **rich.markdown.Markdown.elements,
    'fence': UnpaddedCodeBlock,
    'code_block': UnpaddedCodeBlock,
  }

  def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
    return Measurement.get(console, options, Text(self.markup))


# render width at which nothing soft-wraps, so only explicit line breaks remain
_LOGICAL_RENDER_WIDTH = 16384


class SelectableRichVisual(RichVisual):
  """`RichVisual` that participates in text selection.

  plain `RichVisual` ignores selection entirely: its strips carry no offset
  meta (so mouse hit-testing finds no text and a drag inside the widget
  selects nothing), the selection highlight is never painted, and there is no
  text to extract. stamp the offsets, paint the selected span, and extract
  copies through `Reflow` — the display lines aligned with a second, unwrapped
  render — so copied text keeps the content's own line breaks instead of the
  wrap points and padding of the on-screen rectangle.
  """

  def __init__(self, widget: Widget, renderable: RenderableType):
    super().__init__(widget, renderable)
    self._logical_lines: Optional[list[str]] = None
    self._reflow: Optional[Reflow] = None

  def render_strips(
    self, width: int, height: Optional[int], style: Style, options: RenderOptions
  ) -> list[Strip]:
    strips = super().render_strips(width, height, style, options)
    if self._logical_lines is None:
      self._logical_lines = self._render_logical_lines()
    self._reflow = Reflow([strip.text for strip in strips], self._logical_lines)
    return [
      self._highlight(strip, y, options).apply_offsets(0, y) for y, strip in enumerate(strips)
    ]

  def extract_selection(self, selection: Selection) -> Optional[str]:
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

  def _highlight(self, strip: Strip, y: int, options: RenderOptions) -> Strip:
    if options.selection is None or options.selection_style is None:
      return strip
    span = options.selection.get_span(y)
    if span is None:
      return strip
    start, end = span
    if end == -1 or end > strip.cell_length:
      end = strip.cell_length
    if start >= end:
      return strip
    before, selected, after = strip.divide([start, end, strip.cell_length])
    # overlay, not Strip.apply_style: that applies the style as a base, and the
    # widget background baked into the segments would keep overriding it
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
  """one chat bubble body; vertical sender bar per kind, `thinking` in a
  lighter theme (muted text, faded bar) for reasoning events."""

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
    self._visual: Optional[SelectableRichVisual] = None
    if isinstance(text, str):
      # never parse chat text as content markup: Textual's grammar reads any
      # bare `[` as a tag opener and `markup.escape` doesn't cover its full
      # grammar, so escaped text can still crash the compositor (MarkupError)
      super().__init__(Content(text), classes=kind)
    else:
      # pre-rendered content (e.g. a ChatMarkdown reply, the ANSI-decoded cw banner)
      self._visual = SelectableRichVisual(self, text)
      super().__init__(self._visual, classes=kind)

  def get_selection(self, selection: Selection) -> Optional[tuple[str, str]]:
    # a rich-renderable bubble extracts through its visual's reflow;
    # the plain-string case is Static's default (Content) extraction
    if self._visual is None:
      return super().get_selection(selection)
    extracted = self._visual.extract_selection(selection)
    if extracted is None:
      return None
    return extracted, '\n'


class SystemBubble(Static):
  """dim full-width line for a trace event (reasoning / tool call / tool result).

  no border, no timestamp, no max-width — these are background activity that
  sits between user and bro bubbles. styled muted so the chat stays readable.
  """

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
    # markup=False for the reason on MessageBubble: escaping can't make
    # arbitrary trace text safe for the content-markup parser
    super().__init__(text, markup=False)


class BubbleRow(Vertical):
  """a MessageBubble stacked over its timestamp line, aligned left or right.

  each sits in its own full-width align container so the bubble and the
  timestamp hug the row's edge independently — one shared align container
  would align them as a block, pinning the timestamp to the bubble's width.
  """

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

  def __init__(self, bubble: MessageBubble, *, kind: str, when: datetime):
    timestamp = Static(Content.assemble((when.strftime('%H:%M:%S'), 'dim')), classes='timestamp')
    super().__init__(Container(bubble), Container(timestamp), classes=kind)


class DateSeparator(Static):
  """centered date label shown when the day changes."""

  DEFAULT_CSS = """
  DateSeparator {
    height: 1;
    content-align: center middle;
    color: $text-muted;
    margin: 1 0;
  }
  """

  def __init__(self, when: date):
    super().__init__(when.strftime(DATE_FORMAT))


def _typing_status(pending_tool_calls: list[str], phase_seconds: float) -> str:
  if len(pending_tool_calls) == 0:
    return f'Thinking for {humanize.naturaldelta(phase_seconds)}'
  if len(pending_tool_calls) == 1:
    name = canonical_name(pending_tool_calls[0])
    if phase_seconds > 1:
      return f'Calling {name} for {humanize.naturaldelta(phase_seconds)}'
    return f'Calling {name}()'
  return f'Calling {len(pending_tool_calls)} tools'


class TypingIndicator(Container):
  """left-aligned status bubble animated by ChatApp's interval: 'Thinking for
  <elapsed>' while an LLM roundtrip runs, 'Calling <tool>' / 'Calling N tools'
  while tool results are pending."""

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

  def __init__(self):
    # when the current wait began: the LLM roundtrip's start while no tool call
    # is pending, the front (executing) tool call's start otherwise
    self._phase_since = time.monotonic()
    self._pending_tool_calls: list[str] = []
    self._animation_step = 0
    self._label = Static(self._status())
    super().__init__(self._label)

  def note_tool_call(self, name: str) -> None:
    if len(self._pending_tool_calls) == 0:
      self._phase_since = time.monotonic()
    self._pending_tool_calls.append(name)
    self._label.update(self._status())

  def note_tool_result(self) -> None:
    # results arrive in call order (the provider executes a batch
    # sequentially), so the finished call is the front of the queue
    self._pending_tool_calls.pop(0)
    self._phase_since = time.monotonic()
    self._label.update(self._status())

  def tick(self) -> None:
    self._animation_step = (self._animation_step + 1) % 4
    self._label.update(self._status())

  def _status(self) -> str:
    status = _typing_status(self._pending_tool_calls, time.monotonic() - self._phase_since)
    return status + '.' * self._animation_step


class StatsScreen(ModalScreen):
  """modal showing the bro's info card; dismiss with any key."""

  DEFAULT_CSS = """
  StatsScreen {
    align: center middle;
  }
  StatsScreen > Container {
    width: 80%;
    max-width: 100;
    height: auto;
    max-height: 80%;
    background: $panel;
    border: round $primary;
    padding: 1 2;
  }
  """

  def __init__(self, card: str):
    super().__init__()
    self._card = card

  def compose(self) -> ComposeResult:
    with Container():
      yield Static(self._card, markup=False)

  def on_key(self, event) -> None:
    self.dismiss()


class MessageInput(TextArea):
  """multi-line message field: Enter submits the text as `Submitted`, Shift+Enter
  breaks the line. Shift+Enter arrives as its own key only from terminals speaking
  the kitty keyboard protocol; elsewhere it is indistinguishable from Enter and
  submits."""

  class Submitted(Message):
    def __init__(self, text: str):
      super().__init__()
      self.text = text

  async def _on_key(self, event: events.Key) -> None:
    if event.key == 'enter':
      event.stop()
      event.prevent_default()
      self.post_message(self.Submitted(self.text))
    elif event.key == 'shift+enter':
      event.stop()
      event.prevent_default()
      start, end = self.selection
      self.replace('\n', start, end, maintain_selection_offset=False)


class ChatApp(App):
  CSS = """
  Screen {
    layers: base;
  }
  #history {
    height: 1fr;
    padding: 1 0;
  }
  #input-bar {
    dock: bottom;
    height: auto;
    max-height: 9;
    border: round $primary;
  }
  """

  BINDINGS: ClassVar = [
    Binding('ctrl+d', 'quit', show=False, priority=True),
    Binding('grave_accent', 'show_stats', show=False, priority=True),
    Binding('escape', 'interrupt', show=False, priority=True),
    Binding('ctrl+c', 'interrupt', show=False, priority=True),
  ]

  def __init__(
    self,
    bro: Bro,
    initial: Optional[str],
    history: Optional[list[HistoryMessage]] = None,
    hold: str = 'guided',
  ):
    super().__init__()
    self._bro = bro
    self._initial = initial
    self._hold = hold
    self._history = history if history is not None else []
    self._last_date: Optional[date] = None
    self._typing: Optional[TypingIndicator] = None
    # the worker running the current turn; None between turns
    self._turn: Optional[Worker] = None

  def compose(self) -> ComposeResult:
    yield VerticalScroll(id='history')
    yield MessageInput(placeholder=_IDLE_PLACEHOLDER, highlight_cursor_line=False, id='input-bar')

  async def on_mount(self) -> None:
    self.query_one('#input-bar', MessageInput).focus()
    self.set_interval(0.4, self._tick_typing, pause=False)
    # a resumed conversation's prior exchanges come first, under their own
    # date separators; the banner then opens the live session
    for message in self._history:
      if message.by_user:
        self._append_user_message(message.text, when=message.when)
      else:
        self.append_bro_message(message.text, when=message.when)
    self._append_banner()
    if self._initial is not None and len(self._initial) > 0:
      self._submit(self._initial)

  def _append_banner(self) -> None:
    """opening bro bubble: the cw banner (session environment facts), shown
    before the user's first message. display-only — not part of the bro's
    conversation. the visual banner carries ANSI, decoded for Rich here."""
    from bro.workspace.banner import render_banner

    self._maybe_add_date_separator(date.today())
    # pass the bro name so the logo shows on an in-process (--in-place) run, whose
    # environment doesn't carry this bro's CW_BRO.
    bubble = MessageBubble(Text.from_ansi(render_banner(llm=False, bro=self._bro.name)), kind='bro')
    self._mount_in_history(BubbleRow(bubble, kind='bro', when=datetime.now()))

  def _submit(self, text: str) -> None:
    self._append_user_message(text)
    self._begin_turn()
    self._turn = self._send_to_bro(text)

  async def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
    if len(event.text.strip()) == 0 or self._turn is not None:
      return
    self.query_one('#input-bar', MessageInput).clear()
    self._submit(event.text)

  def on_text_selected(self) -> None:
    # copy-on-select (OSC 52); posted on every mouse-up, so a plain click
    # must leave the clipboard alone
    selected = self.screen.get_selected_text()
    if selected is not None:
      self.copy_to_clipboard(selected)

  def _append_user_message(self, text: str, when: Optional[datetime] = None) -> None:
    when = when if when is not None else datetime.now()
    self._maybe_add_date_separator(when.date())
    bubble = MessageBubble(text, kind='user')
    self._mount_in_history(BubbleRow(bubble, kind='user', when=when))

  def append_bro_message(self, text: str, when: Optional[datetime] = None) -> None:
    when = when if when is not None else datetime.now()
    self._maybe_add_date_separator(when.date())
    # bro messages are markdown-authored — render them (bold, lists, fenced
    # code, hyperlinks) instead of showing the raw syntax.
    bubble = MessageBubble(ChatMarkdown(text), kind='bro')
    self._mount_in_history(BubbleRow(bubble, kind='bro', when=when))

  def _append_error_message(self, text: str, when: Optional[datetime] = None) -> None:
    when = when if when is not None else datetime.now()
    self._maybe_add_date_separator(when.date())
    bubble = MessageBubble(text, kind='error')
    self._mount_in_history(BubbleRow(bubble, kind='error', when=when))

  def append_thinking(self, text: str) -> None:
    bubble = MessageBubble(ChatMarkdown(text), kind='thinking')
    self._mount_in_history(BubbleRow(bubble, kind='thinking', when=datetime.now()))

  def append_trace_line(self, text: str) -> None:
    self._mount_in_history(SystemBubble(text))

  def _maybe_add_date_separator(self, day: date) -> None:
    if self._last_date != day:
      self._mount_in_history(DateSeparator(day))
      self._last_date = day

  def _mount_in_history(self, widget: Widget) -> None:
    # everything mid-turn joins the history stream, where the typing indicator
    # sits too — mount above it so it keeps its place at the bottom.
    self.query_one('#history', VerticalScroll).mount(
      widget, before=self._typing if self._typing is not None else None
    )
    self._scroll_to_end()

  def _scroll_to_end(self) -> None:
    self.call_after_refresh(
      lambda: self.query_one('#history', VerticalScroll).scroll_end(animate=False)
    )

  def _begin_turn(self) -> None:
    # the field takes no text while the bro works — one conversation is never
    # driven by two turns — so an interrupt is the way back to it.
    field = self.query_one('#input-bar', MessageInput)
    field.placeholder = _BUSY_PLACEHOLDER
    field.disabled = True
    if self._typing is None:
      self._typing = TypingIndicator()
      self.query_one('#history', VerticalScroll).mount(self._typing)
    self._scroll_to_end()

  def _end_turn(self) -> None:
    self._turn = None
    if self._typing is not None:
      self._typing.remove()
      self._typing = None
    field = self.query_one('#input-bar', MessageInput)
    field.disabled = False
    field.placeholder = _IDLE_PLACEHOLDER
    field.focus()

  def _tick_typing(self) -> None:
    if self._typing is None:
      return
    self._typing.tick()

  def note_tool_call(self, name: str) -> None:
    if self._typing is not None:
      self._typing.note_tool_call(name)

  def note_tool_result(self) -> None:
    if self._typing is not None:
      self._typing.note_tool_result()

  @work(exclusive=True)
  async def _send_to_bro(self, text: str) -> None:
    # async, on the app's own loop: cancelling this worker unwinds the agent
    # loop, while a thread worker would keep running, cancelled or not.
    observer = TUIRenderer(self)
    try:
      reply = await self._bro.send(text, observer=observer, surface='call', hold=self._hold)
    except asyncio.CancelledError:
      # a cancellation raised by the app's own teardown finds no UI left to update
      if self.is_running:
        self._end_turn()
        self.append_trace_line(INTERRUPTED_NOTICE)
      raise
    except Exception as error:
      self._end_turn()
      self._append_error_message(f'{type(error).__name__}: {error}')
      return
    self._end_turn()
    self.append_bro_message(reply)

  async def action_interrupt(self) -> None:
    await self._interrupt_turn()

  async def action_quit(self) -> None:
    # the turn goes down with the UI: an abandoned one keeps the process alive
    # after the terminal is back.
    await self._interrupt_turn()
    await super().action_quit()

  async def _interrupt_turn(self) -> bool:
    """cancel the running turn and wait for it to unwind; False when idle."""
    worker = self._turn
    if worker is None:
      return False
    worker.cancel()
    with contextlib.suppress(WorkerCancelled, WorkerFailed):
      await worker.wait()
    return True

  async def action_show_stats(self) -> None:
    card = await format_card(self._bro, include_system_prompt=False)
    await self.push_screen(StatsScreen(card))


class TUIRenderer(Observer):
  """post observed events into a `ChatApp` — assistant text as bro bubbles,
  reasoning as thinking bubbles, the tool trace as dim `SystemBubble` rows.

  the bro runs as an async worker on the app's own loop, so each callback
  mounts its bubble directly.
  """

  def __init__(self, app: 'ChatApp'):
    self._app = app

  def _post(self, text: str) -> None:
    self._app.append_trace_line(text)

  def on_event(self, event: ObservedEvent) -> None:
    if isinstance(event, ReasoningEvent):
      # each event carries one complete reasoning-summary block, so it renders
      # whole in its own bubble, untruncated
      self._app.append_thinking(event.content)
    elif isinstance(event, InterimAssistantTextEvent):
      self._app.append_bro_message(event.content)
    elif isinstance(event, ToolCallEvent):
      self._app.note_tool_call(event.tool_name)
      self._post(f'→ {format_tool_call(event.tool_name, event.arguments)}')
    elif isinstance(event, ToolResultEvent):
      self._app.note_tool_result()
      self._post(f'← {canonical_name(event.tool_name)}')
    elif isinstance(
      event, (TurnStartedEvent, TurnCompletedEvent, TurnRefusedEvent, TurnFailedEvent)
    ):
      return
    else:
      assert_never(event)
