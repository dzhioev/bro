"""IM-style chat TUI for `call`. Entry point: `ChatApp(bro, initial).run()`."""

import asyncio
from datetime import date, datetime
from typing import Any, ClassVar, Optional

import rich.markdown
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, RichVisual
from textual.widget import Widget
from textual.widgets import Input, Static

from bro.bros.bro import Bro
from bro.show import format_card
from do._reflow import Reflow
from do._trace_format import compact_value, oneline, truncate
from do.call import DATE_FORMAT
from do.resume import HistoryMessage
from llm.observer import Observer

_TRACE_VALUE_LIMIT = 200


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
  """one chat bubble; left or right vertical bar per sender, timestamp in corner."""

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
  """

  def __init__(self, text: RenderableType, *, by_user: bool, when: datetime):
    classes = 'user' if by_user else 'bro'
    timestamp = when.strftime('%H:%M')
    self._visual: Optional[SelectableRichVisual] = None
    if isinstance(text, str):
      # never parse chat text as content markup: Textual's grammar reads any
      # bare `[` as a tag opener and `markup.escape` doesn't cover its full
      # grammar, so escaped text can still crash the compositor (MarkupError)
      super().__init__(Content.assemble(text, '\n', (timestamp, 'dim')), classes=classes)
    else:
      # pre-rendered content (e.g. a ChatMarkdown reply, the ANSI-decoded cw
      # banner); append the timestamp as a dim line without running it through
      # markup parsing
      self._visual = SelectableRichVisual(self, Group(text, Text(timestamp, style='dim')))
      super().__init__(self._visual, classes=classes)

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


class BubbleRow(Container):
  """horizontal row containing a single MessageBubble; aligns left or right."""

  DEFAULT_CSS = """
  BubbleRow {
    width: 100%;
    height: auto;
    margin: 0 1 1 1;
  }
  BubbleRow.user {
    align: right top;
  }
  BubbleRow.bro {
    align: left top;
  }
  """

  def __init__(self, bubble: MessageBubble, *, by_user: bool):
    super().__init__(bubble, classes='user' if by_user else 'bro')


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


class TypingIndicator(Container):
  """left-aligned 'Typing.../..' bubble animated by ChatApp's interval."""

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
    self._label = Static('Typing')
    super().__init__(self._label)

  def set_text(self, text: str) -> None:
    self._label.update(text)


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
    height: 3;
    border: round $primary;
  }
  """

  BINDINGS: ClassVar = [
    Binding('ctrl+d', 'quit', show=False, priority=True),
    Binding('grave_accent', 'show_stats', show=False, priority=True),
  ]

  def __init__(
    self, bro: Bro, initial: Optional[str], history: Optional[list[HistoryMessage]] = None
  ):
    super().__init__()
    self._bro = bro
    self._initial = initial
    self._history = history if history is not None else []
    self._last_date: Optional[date] = None
    self._typing: Optional[TypingIndicator] = None
    self._typing_step = 0

  def compose(self) -> ComposeResult:
    yield VerticalScroll(id='history')
    yield Input(placeholder='message…', id='input-bar')

  async def on_mount(self) -> None:
    self.query_one('#input-bar', Input).focus()
    self.set_interval(0.4, self._tick_typing, pause=False)
    # a resumed conversation's prior exchanges come first, under their own
    # date separators; the banner then opens the live session
    for message in self._history:
      if message.by_user:
        self._append_user_message(message.text, when=message.when)
      else:
        self._append_bro_message(message.text, when=message.when)
    self._append_banner()
    if self._initial is not None and len(self._initial) > 0:
      await self._submit(self._initial)

  def _append_banner(self) -> None:
    """opening bro bubble: the cw banner (session environment facts), shown
    before the user's first message. display-only — not part of the bro's
    conversation. the visual banner carries ANSI, decoded for Rich here."""
    from cw import render_banner

    self._maybe_add_date_separator(date.today())
    # pass the bro name so the logo shows on an in-process (--host) run, whose
    # environment doesn't carry this bro's CW_BRO.
    bubble = MessageBubble(
      Text.from_ansi(render_banner(llm=False, bro=self._bro.name)),
      by_user=False,
      when=datetime.now(),
    )
    self.query_one('#history', VerticalScroll).mount(BubbleRow(bubble, by_user=False))
    self._scroll_to_end()

  async def _submit(self, text: str) -> None:
    self._append_user_message(text)
    self._show_typing()
    self._send_to_bro(text)

  async def on_input_submitted(self, event: Input.Submitted) -> None:
    text = event.value
    if len(text) == 0:
      return
    event.input.value = ''
    await self._submit(text)

  def on_text_selected(self) -> None:
    # copy-on-select (OSC 52); posted on every mouse-up, so a plain click
    # must leave the clipboard alone
    selected = self.screen.get_selected_text()
    if selected is not None:
      self.copy_to_clipboard(selected)

  def _append_user_message(self, text: str, when: Optional[datetime] = None) -> None:
    when = when if when is not None else datetime.now()
    self._maybe_add_date_separator(when.date())
    bubble = MessageBubble(text, by_user=True, when=when)
    self.query_one('#history', VerticalScroll).mount(BubbleRow(bubble, by_user=True))
    self._scroll_to_end()

  def _append_bro_message(self, text: str, when: Optional[datetime] = None) -> None:
    when = when if when is not None else datetime.now()
    self._maybe_add_date_separator(when.date())
    # replies are markdown-authored — render them (bold, lists, fenced code,
    # hyperlinks) instead of showing the raw syntax.
    bubble = MessageBubble(ChatMarkdown(text), by_user=False, when=when)
    self.query_one('#history', VerticalScroll).mount(BubbleRow(bubble, by_user=False))
    self._scroll_to_end()

  def append_trace_line(self, text: str) -> None:
    """mount a dim system bubble; called from `TUIRenderer` via `call_from_thread`."""
    # the trace lives in the history stream, so the typing indicator (which is
    # also mounted there) needs to slide back to the bottom after each event.
    self.query_one('#history', VerticalScroll).mount(
      SystemBubble(text), before=self._typing if self._typing is not None else None
    )
    self._scroll_to_end()

  def _maybe_add_date_separator(self, day: date) -> None:
    if self._last_date != day:
      self.query_one('#history', VerticalScroll).mount(DateSeparator(day))
      self._last_date = day

  def _scroll_to_end(self) -> None:
    self.call_after_refresh(
      lambda: self.query_one('#history', VerticalScroll).scroll_end(animate=False)
    )

  def _show_typing(self) -> None:
    if self._typing is not None:
      return
    self._typing = TypingIndicator()
    self._typing_step = 0
    self.query_one('#history', VerticalScroll).mount(self._typing)
    self._scroll_to_end()

  def _hide_typing(self) -> None:
    if self._typing is None:
      return
    self._typing.remove()
    self._typing = None

  def _tick_typing(self) -> None:
    if self._typing is None:
      return
    self._typing_step = (self._typing_step + 1) % 4
    self._typing.set_text('Typing' + ('.' * self._typing_step))

  @work(thread=True, exclusive=True)
  def _send_to_bro(self, text: str) -> None:
    # run in a thread so the OpenAI client's blocking `responses.create` call
    # doesn't freeze the Textual event loop (no user-bubble paint, no typing
    # animation). bridge UI updates back via call_from_thread.
    observer = TUIRenderer(self)
    try:
      reply = asyncio.run(self._bro.send(text, observer=observer, entry_point='call'))
    except Exception as e:
      reply = f'[error] {type(e).__name__}: {e}'
    self.call_from_thread(self._on_reply, reply)

  def _on_reply(self, reply: str) -> None:
    self._hide_typing()
    self._append_bro_message(reply)

  async def action_show_stats(self) -> None:
    card = await format_card(self._bro, include_system_prompt=False)
    await self.push_screen(StatsScreen(card))


class TUIRenderer(Observer):
  """post observed events into a `ChatApp` as dim `SystemBubble` rows.

  the bro runs in a Textual worker thread; each callback hops onto the app
  thread via `call_from_thread` to mount the bubble safely.
  """

  def __init__(self, app: 'ChatApp'):
    self._app = app

  def _post(self, text: str) -> None:
    self._app.call_from_thread(self._app.append_trace_line, text)

  def on_reasoning(self, text: str) -> None:
    self._post(f'✎ thinking: {truncate(oneline(text), _TRACE_VALUE_LIMIT, overflow_marker=False)}')

  def on_assistant_message(self, text: str, terminal: bool) -> None:
    # skip terminal — ChatApp mounts the reply as a bro bubble via _on_reply,
    # so emitting here would double-render.
    if terminal:
      return
    self._post(f'✎ says: {truncate(oneline(text), _TRACE_VALUE_LIMIT, overflow_marker=False)}')

  def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
    self._post(
      f'→ {name} {truncate(compact_value(arguments), _TRACE_VALUE_LIMIT, overflow_marker=False)}'
    )

  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None:
    self._post(
      f'← {name} {truncate(compact_value(result), _TRACE_VALUE_LIMIT, overflow_marker=False)}'
    )
