"""IM-style chat TUI for `call`. Entry point: `ChatApp(bro, initial).run()`."""

import asyncio
from datetime import date, datetime

from rich.markup import escape as rich_escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from bro.bro import BaseBro
from bro.show import format_card


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

  def __init__(self, text: str, *, by_user: bool, when: datetime):
    classes = 'user' if by_user else 'bro'
    ts = when.strftime('%H:%M')
    body = f'{rich_escape(text)}\n[dim]{ts}[/dim]'
    super().__init__(body, classes=classes, markup=True)


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
    super().__init__(when.strftime('%a, %b %-d, %Y'))


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
      yield Static(self._card)

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

  BINDINGS = [
    Binding('ctrl+d', 'quit', show=False, priority=True),
    Binding('grave_accent', 'show_stats', show=False, priority=True),
  ]

  def __init__(self, bro: BaseBro, initial: str):
    super().__init__()
    self._bro = bro
    self._initial = initial
    self._last_date: date | None = None
    self._typing: TypingIndicator | None = None
    self._typing_step = 0

  def compose(self) -> ComposeResult:
    yield VerticalScroll(id='history')
    yield Input(placeholder='message…', id='input-bar')

  async def on_mount(self) -> None:
    self.query_one('#input-bar', Input).focus()
    self.set_interval(0.4, self._tick_typing, pause=False)
    if len(self._initial) > 0:
      await self._submit(self._initial)

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

  def _append_user_message(self, text: str) -> None:
    self._maybe_add_date_separator()
    bubble = MessageBubble(text, by_user=True, when=datetime.now())
    self.query_one('#history', VerticalScroll).mount(BubbleRow(bubble, by_user=True))
    self._scroll_to_end()

  def _append_bro_message(self, text: str) -> None:
    self._maybe_add_date_separator()
    bubble = MessageBubble(text, by_user=False, when=datetime.now())
    self.query_one('#history', VerticalScroll).mount(BubbleRow(bubble, by_user=False))
    self._scroll_to_end()

  def _maybe_add_date_separator(self) -> None:
    today = date.today()
    if self._last_date != today:
      self.query_one('#history', VerticalScroll).mount(DateSeparator(today))
      self._last_date = today

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
    try:
      reply = asyncio.run(self._bro.send(text))
    except Exception as e:
      reply = f'[error] {type(e).__name__}: {e}'
    self.call_from_thread(self._on_reply, reply)

  def _on_reply(self, reply: str) -> None:
    self._hide_typing()
    self._append_bro_message(reply)

  async def action_show_stats(self) -> None:
    card = await format_card(self._bro, include_system_prompt=False)
    await self.push_screen(StatsScreen(card))
