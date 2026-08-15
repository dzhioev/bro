"""Textual application shell embedding the trails conversation view."""

import asyncio
import contextlib
from contextlib import ExitStack
from datetime import datetime
from typing import ClassVar, Optional

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from bros.bro import Bro
from bro.launch.call import INTERRUPTED_NOTICE
from bro.show import format_card
from bro.trails.display import (
  DisplayRecord,
  DisplaySession,
  Error,
  LiveDisplayObserver,
  Notice,
  Origin,
  PresetName,
  preset,
)
from bro.trails.display.textual import TextualRenderer, TrailView

_IDLE_PLACEHOLDER = 'message…'
_BUSY_PLACEHOLDER = 'esc to interrupt…'


class StatsScreen(ModalScreen):
  """Modal showing the bro's info card; dismiss with any key."""

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
  """Multi-line field where Enter submits and Shift+Enter inserts a line break."""

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
    history: Optional[list[DisplayRecord]] = None,
    hold: str = 'guided',
    preset_name: PresetName = PresetName.CALL,
  ):
    super().__init__()
    self._bro = bro
    self._initial = initial
    self._hold = hold
    self._history = history if history is not None else []
    self._preset_name = preset_name
    self._turn: Worker | None = None
    self._display_lifetime = ExitStack()
    self._display_session: DisplaySession | None = None
    self._observer: LiveDisplayObserver | None = None
    self._surface_sequence = 0

  def compose(self) -> ComposeResult:
    yield TrailView(id='history')
    yield MessageInput(placeholder=_IDLE_PLACEHOLDER, highlight_cursor_line=False, id='input-bar')

  async def on_mount(self) -> None:
    self.query_one('#input-bar', MessageInput).focus()
    renderer = TextualRenderer(self.query_one('#history', TrailView))
    configuration = preset(self._preset_name, context_label=self._bro.name)
    self._display_session = self._display_lifetime.enter_context(
      DisplaySession(configuration, renderer)
    )
    self._observer = LiveDisplayObserver(
      self._display_session,
      activity_id='turn',
    )
    self._display_session.consume(self._history)
    self._display_session.consume(self._banner_notice())
    if self._initial is not None and len(self._initial) > 0:
      self._submit(self._initial)

  def on_unmount(self) -> None:
    self._display_lifetime.close()

  def on_text_selected(self) -> None:
    selected = self.screen.get_selected_text()
    if selected is not None:
      self.copy_to_clipboard(selected)

  def _banner_notice(self) -> Notice:
    from bro.workspace.banner import render_banner

    return Notice(
      key=self._surface_key('banner'),
      origin=Origin.SURFACE,
      timestamp=datetime.now().astimezone().isoformat(),
      content=render_banner(llm=False, bro=self._bro.name),
      trusted_visual=True,
    )

  def _surface_key(self, kind: str) -> str:
    sequence = self._surface_sequence
    self._surface_sequence += 1
    return f'surface:{kind}:{sequence}'

  def _submit(self, text: str) -> None:
    self._begin_turn()
    self._turn = self._send_to_bro(text)

  async def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
    if len(event.text.strip()) == 0 or self._turn is not None:
      return
    self.query_one('#input-bar', MessageInput).clear()
    self._submit(event.text)

  def _begin_turn(self) -> None:
    field = self.query_one('#input-bar', MessageInput)
    field.placeholder = _BUSY_PLACEHOLDER
    field.disabled = True

  def _end_turn(self) -> None:
    self._turn = None
    field = self.query_one('#input-bar', MessageInput)
    field.disabled = False
    field.placeholder = _IDLE_PLACEHOLDER
    field.focus()

  @work(exclusive=True)
  async def _send_to_bro(self, text: str) -> None:
    observer = self._observer
    session = self._display_session
    if observer is None or session is None:
      raise RuntimeError('chat display is not mounted')
    try:
      await self._bro.send(text, observer=observer, surface='call', hold=self._hold)
    except asyncio.CancelledError:
      if self.is_running:
        observer.close_activity()
        session.consume(
          Notice(
            key=self._surface_key('interruption'),
            origin=Origin.SURFACE,
            timestamp=datetime.now().astimezone().isoformat(),
            content=INTERRUPTED_NOTICE,
            level='interruption',
          )
        )
        self._end_turn()
      raise
    except Exception as error:
      observer.close_activity()
      if not observer.turn_finished:
        session.consume(
          Error(
            key=self._surface_key('error'),
            origin=Origin.SURFACE,
            timestamp=datetime.now().astimezone().isoformat(),
            content=f'{type(error).__name__}: {error}',
          )
        )
      self._end_turn()
      return
    self._end_turn()

  async def action_interrupt(self) -> None:
    await self._interrupt_turn()

  async def action_quit(self) -> None:
    await self._interrupt_turn()
    self._display_lifetime.close()
    await super().action_quit()

  async def _interrupt_turn(self) -> bool:
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
