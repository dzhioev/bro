import asyncio
import json
import signal
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

import bro.llm.llms.echo as llm_llms_echo
import bro.llm.llms.openai as llm_llms_openai
from bro.bro import AnswerDelivered, BaseBro
from bro.launch.call import call_text, chat_main
from bro.llm.llm import NativeLLMSpec
from bro.llm.mcp import MCPServer
from bro.llm.observer import (
  InterimAssistantTextEvent,
  ToolCallEvent,
  ToolResultEvent,
  TurnCompletedEvent,
  TurnStartedEvent,
)
from bro.native.llm import LLM
from bro.native.runner import Runner
from bro.trails.display import (
  AssistantText,
  DisplayRecord,
  Notice,
  Origin,
  PresetName,
  Reasoning,
  UserInput,
)
from bro.trails.display.textual import (
  BubbleRow,
  ChatMarkdown,
  MessageBubble,
  SystemBubble,
  TypingIndicator,
  _typing_status,
)
from bros.bro import Bro


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    self.send_calls.append(messages)
    return self.response


class RecordBro(Bro):
  name = 'record'
  description = 'records inputs'
  llm_spec = llm_llms_echo.LLMSpec()

  def __init__(self):
    super().__init__(system_prompt='record')


class _MockRunner(Runner):
  """drives a declaration through a MockLLM, so the call surfaces under test
  need no provider."""

  def __init__(self, bro: Optional[BaseBro] = None, response: str = 'reply'):
    super().__init__(bro if bro is not None else RecordBro())
    self.mock_llm = MockLLM(response=response)

  def _create_llm(self, *, hold: str) -> LLM:
    return self.mock_llm


class _ScriptedLines:
  """callable that yields each scripted line in turn, then raises EOFError."""

  def __init__(self, lines: list[str]):
    self._lines = list(lines)

  def __call__(self) -> str:
    if len(self._lines) == 0:
      raise EOFError
    return self._lines.pop(0)


def _fixed_now() -> datetime:
  return datetime(2026, 5, 28, 12, 34, 56)


def _consume_display(app, record) -> None:
  assert app._display_session is not None
  app._display_session.consume(record)


@pytest.mark.asyncio
async def test_text_drives_send_until_eof(capsys):
  runner = _MockRunner(response='reply')
  await call_text(
    runner,
    'first',
    read_line=_ScriptedLines(['second', 'third']),
    now=_fixed_now,
  )
  assert len(runner.mock_llm.send_calls) == 3
  assert runner.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'first'}
  assert runner.mock_llm.send_calls[1] == [{'role': 'user', 'content': 'second'}]
  assert runner.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'third'}]
  out = capsys.readouterr().out
  # each reply line is `[HH:MM:SS] <bro-name>: <reply>`
  assert out.count('[12:34:56] record: reply') == 3


@pytest.mark.asyncio
async def test_text_emits_banner_before_first_reply(capsys, monkeypatch):
  monkeypatch.setattr(
    'bro.workspace.banner.render_banner', lambda llm=False, bro=None: f'BANNER[{bro}]'
  )
  runner = _MockRunner(response='reply')
  await call_text(runner, 'first', read_line=_ScriptedLines([]), now=_fixed_now)
  out = capsys.readouterr().out
  # banner is the opening bro message, before the first reply line; the bro name
  # is passed through so the logo renders on an in-process run, whose
  # environment doesn't carry this bro's RIDE_BRO
  assert out.index('BANNER[record]') < out.index('[12:34:56] record: reply')
  assert '[12:34:56] record:\nBANNER[record]' in out


@pytest.mark.asyncio
async def test_text_skips_empty_input(capsys):
  runner = _MockRunner(response='reply')
  await call_text(
    runner,
    'first',
    read_line=_ScriptedLines(['', '   ', 'real']),
    now=_fixed_now,
  )
  # empty string skipped; whitespace-only is sent through (boundary belongs upstream)
  assert len(runner.mock_llm.send_calls) == 3  # first + '   ' + 'real'
  assert runner.mock_llm.send_calls[1] == [{'role': 'user', 'content': '   '}]
  assert runner.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'real'}]


@pytest.mark.asyncio
async def test_text_returns_on_immediate_eof(capsys):
  runner = _MockRunner(response='reply')
  await call_text(runner, 'only', read_line=_ScriptedLines([]), now=_fixed_now)
  assert len(runner.mock_llm.send_calls) == 1
  assert runner.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'only'}


@dataclass(frozen=True)
class _FastlessSpec(NativeLLMSpec):
  """test spec that intentionally has no fast-mode equivalent."""

  TYPE: ClassVar[str] = 'fastless'

  model: str = 'whatever'

  def dump(self) -> dict:
    return {'type': self.TYPE, 'model': self.model}

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'NativeLLMSpec':
    return cls(model=data['model'])


class _ChatBro(Bro):
  name = 'record'
  description = 'records inputs'
  llm_spec = llm_llms_openai.LLMSpec(model='gpt-5.4-mini')

  def __init__(self):
    super().__init__(system_prompt='record')


class _FastlessBro(Bro):
  name = 'fastless'
  description = 'has a spec without fast support'
  llm_spec = _FastlessSpec(model='whatever')

  def __init__(self):
    super().__init__(system_prompt='fastless')


def _chat(argv: list[str]):
  return chat_main(argv, program=['bro', 'chat'])


def test_bro_chat_default_builds_plain_spec(monkeypatch):
  built: list[Runner] = []

  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    built.append(runner)

  monkeypatch.setattr('bro.registry.create_bro', lambda name: _ChatBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record', 'hi']) is None
  spec = built[0].bro.llm_spec
  assert isinstance(spec, llm_llms_openai.LLMSpec)
  assert spec.service_tier is None


def test_bro_chat_fast_flag_is_explicit(monkeypatch):
  built: list[Runner] = []

  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    built.append(runner)

  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record', 'hi', '--fast']) is None
  spec = built[0].bro.llm_spec
  assert isinstance(spec, llm_llms_openai.LLMSpec)
  assert spec.service_tier == 'priority'


def test_chat_starts_an_empty_repl_and_defaults_to_guided(monkeypatch):
  captured: list[tuple[Optional[str], str, PresetName]] = []

  async def fake_call_text(
    runner,
    initial: Optional[str],
    history=None,
    hold: str = 'guided',
    preset_name: PresetName = PresetName.CHAT,
  ):
    captured.append((initial, hold, preset_name))

  monkeypatch.setattr('bro.registry.create_bro', lambda name: _ChatBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record']) is None
  assert captured == [(None, 'guided', PresetName.CHAT)]


def test_chat_relays_a_delivered_answer_to_the_summoner(monkeypatch):
  async def deliver(*args, **kwargs):
    raise AnswerDelivered('the verdict')

  channel = MagicMock()
  monkeypatch.setattr('bro.registry.create_bro', lambda name: _ChatBro())
  monkeypatch.setattr('bro.launch.call.call_text', deliver)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)
  monkeypatch.setattr('bro.launch.call.RunLifecycle.from_env', staticmethod(lambda: channel))

  assert _chat(['bro', 'record']) == 0
  channel.completed.assert_called_once_with('the verdict', 'ok')
  channel.close.assert_called_once_with()


def test_chat_fails_a_delivered_answer_without_a_channel(monkeypatch, capsys):
  async def deliver(*args, **kwargs):
    raise AnswerDelivered('the verdict')

  monkeypatch.setattr('bro.registry.create_bro', lambda name: _ChatBro())
  monkeypatch.setattr('bro.launch.call.call_text', deliver)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)
  monkeypatch.setattr('bro.launch.call.RunLifecycle.from_env', staticmethod(lambda: None))

  assert _chat(['bro', 'record']) == 1
  assert 'cannot reach the summoner' in capsys.readouterr().err


@pytest.mark.parametrize(
  'flag',
  ['--summon', '--grant', '--revoke', '--into', '--no-trails', '--host'],
)
def test_runtime_flags_are_not_accepted(flag):
  with pytest.raises(SystemExit):
    _chat(['bro', 'record', 'hi', flag])


def test_at_requires_fork(capsys):
  assert _chat(['bro', 'record', 'hi', '--at', '7']) == 1
  assert 'requires --fork' in capsys.readouterr().err


def test_fork_runs_the_recorded_history_under_the_current_spec(monkeypatch, capsys):
  from bro.launch.resume import ResumedCall
  from bro.trails.display import RecordedSource

  captured: dict = {}

  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    captured['runner'] = runner
    captured['initial'] = initial
    captured['history'] = history

  forked_runner = _MockRunner()
  forked_runner.trail_id = 'new-trail'
  history: list[DisplayRecord] = [
    UserInput(
      key='recorded:old-trail:message:1:0:0',
      origin=Origin.RECORDED,
      source=RecordedSource('old-trail', 1),
      timestamp='2026-05-27T09:00:00Z',
      content='hello',
    )
  ]

  def fake_resume(client, bro_name, trail_ref, *, llm_spec, at=None):
    captured['trail_ref'] = trail_ref
    captured['llm_spec'] = llm_spec
    captured['at'] = at
    return ResumedCall(runner=forked_runner, history=history, trail_id='old-trail')

  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('bro.launch.resume.resume', fake_resume)
  monkeypatch.setattr('bro.trails.store.default_store', lambda: MagicMock())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record', '--fork', 'trail-1', '--at', '7']) is None
  assert captured['trail_ref'] == 'trail-1'
  assert captured['at'] == 7
  assert captured['llm_spec'] == _ChatBro.llm_spec
  assert captured['runner'] is forked_runner
  assert captured['initial'] is None
  assert captured['history'] is history
  assert 'bro chat record --fork new-trail' in capsys.readouterr().err


def test_bare_fork_selects_the_newest_call(monkeypatch):
  from bro.launch.resume import ResumedCall

  captured: dict = {}

  def fake_resume(client, bro_name, trail_ref, *, llm_spec, at=None):
    captured['trail_ref'] = trail_ref
    return ResumedCall(runner=_MockRunner(), history=[], trail_id='old-trail')

  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    pass

  monkeypatch.setattr('bro.registry.get_class', lambda name: RecordBro)
  monkeypatch.setattr('bro.launch.resume.resume', fake_resume)
  monkeypatch.setattr('bro.trails.store.default_store', lambda: MagicMock())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record', '--fork']) is None
  assert captured['trail_ref'] == 'latest'


def test_prints_fork_hint_when_a_trail_was_recorded(monkeypatch, capsys):
  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    runner.trail_id = 'trail-xyz'

  monkeypatch.setattr('bro.registry.create_bro', lambda name: RecordBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record', 'hi']) is None
  assert 'bro chat record --fork trail-xyz' in capsys.readouterr().err


def test_initial_slash_invocation_passes_through_verbatim(monkeypatch):
  captured: list[str] = []

  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    captured.append(initial)

  monkeypatch.setattr('bro.registry.create_bro', lambda name: RecordBro())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)

  assert _chat(['bro', 'record', '/ask dev to ping']) is None
  assert captured == ['/ask dev to ping']


def test_chat_markdown_carries_bold_and_link_styles():
  from rich.console import Console

  console = Console(width=80)
  segments = console.render(ChatMarkdown('**bold** and [docs](https://example.com/x)'))
  styles = [segment.style for segment in segments if segment.style is not None]
  assert any(style.bold is True for style in styles)
  assert any(style.link == 'https://example.com/x' for style in styles)


def test_chat_markdown_measurement_hugs_short_text():
  from rich.console import Console
  from rich.measure import Measurement

  console = Console(width=80)
  measurement = Measurement.get(console, console.options, ChatMarkdown('ok'))
  assert measurement.maximum == 2


def test_message_bubble_selection_honors_offsets():
  from textual.geometry import Offset
  from textual.selection import Selection

  bubble = MessageBubble('first\nsecond', kind='user')
  extraction = bubble.get_selection(Selection(Offset(0, 1), None))
  assert extraction is not None
  assert extraction[0] == 'second'


@pytest.mark.asyncio
async def test_tui_drag_inside_markdown_bubble_selects_rendered_text(monkeypatch):
  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test(size=(100, 40)) as pilot:
    _consume_display(
      app,
      AssistantText(
        key='reply',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:34:56',
        content='a **bold** reply',
      ),
    )
    await pilot.pause()
    bubble = app.query(MessageBubble).last()
    region = bubble.content_region
    # drag across the first rendered line, strictly inside the bubble — this is
    # the gesture that used to select nothing (rich renderables carry no offset
    # meta, so hit-testing found no text)
    await pilot.mouse_down(offset=(region.x + 2, region.y))
    await pilot.hover(offset=(region.x + 5, region.y))
    await pilot.mouse_up(offset=(region.x + 5, region.y))
    await pilot.pause()
    selection = app.screen.selections.get(bubble)
    assert selection is not None
    # a precise span, not a whole-widget fallback
    assert selection.start is not None
    # markdown renders '**bold**' as 'bold': the drag covers chars 2-5 of the
    # rendered 'a bold reply', release cell included
    assert app.screen.get_selected_text() == 'bold'
    assert app.clipboard == 'bold'
    # the selected span paints the selection background; the rest keeps the base
    backgrounds = {
      segment.text: segment.style.bgcolor
      for segment in bubble.render_line(0)
      if segment.style is not None and segment.style.bgcolor is not None
    }
    assert backgrounds['bold'] != backgrounds['a ']


@pytest.mark.asyncio
async def test_tui_markdown_bubble_copy_reflows_to_logical_lines(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  command = (
    'dive-in --auto --grant notion -t "https://example.com/x" '
    '"a long quoted argument that certainly wraps across the bubble width"'
  )
  paragraph = 'Great — moving to the verification phase next, with the notion grant in place.'
  reply = f'{paragraph}\n\n```\n{command}\n```\n\n```python\ndef f():\n    return 1\n```'
  app = ChatApp(_MockRunner(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    _consume_display(
      app,
      AssistantText(
        key='reply',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:34:56',
        content=reply,
      ),
    )
    await pilot.pause()
    bubble = app.query(MessageBubble).last()
    app.screen.selections = {bubble: SELECT_ALL}
    copied = app.screen.get_selected_text()
    assert copied is not None
    lines = copied.split('\n')
    # wrap points and padding never reach the copy: the paragraph and the fenced
    # command each come back as the single line they were authored as, and the
    # python block keeps its indentation
    assert lines[0] == paragraph
    assert lines[1] == ''
    assert lines[2] == command
    assert lines[3] == ''
    assert lines[4:6] == ['def f():', '    return 1']
    assert all(line == line.rstrip() for line in lines)


@pytest.mark.asyncio
async def test_tui_survives_markup_like_text(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp, StatsScreen

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  # the shape that crashed the compositor: a bare `[` opens what Textual's
  # content-markup grammar reads as a tag with key=value pairs inside, and
  # rich.markup.escape does not neutralize it
  trace = "→ flow::list_tasks [x=1, statuses=['done', 'dropped'], input=3]"
  message = "please retry [a=b, statuses=['done', 'dropped']]"
  app = ChatApp(_MockRunner(), None)
  async with app.run_test(size=(120, 40)) as pilot:
    _consume_display(
      app,
      Notice(
        key='trace',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:34:55',
        content=trace,
        level='interruption',
      ),
    )
    _consume_display(
      app,
      UserInput(
        key='user',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:34:56',
        content=message,
      ),
    )
    # reflow renders every bubble — where the MarkupError used to raise
    await pilot.pause()
    system = app.query(SystemBubble).last()
    app.screen.selections = {system: SELECT_ALL}
    assert app.screen.get_selected_text() == trace
    user = app.query(MessageBubble).last()
    app.screen.selections = {user: SELECT_ALL}
    assert app.screen.get_selected_text() == message
    # the timestamp widget keeps its dim style without a markup parse
    timestamp = app.query('.timestamp').last()
    timestamp_styles = {
      segment.text.strip(): segment.style
      for segment in timestamp.render_line(0)
      if segment.style is not None
    }
    assert timestamp_styles['12:34:56'].dim is True
    # the stats card is arbitrary text too
    await app.push_screen(StatsScreen(f'card {trace}'))
    await pilot.pause()


@pytest.mark.asyncio
async def test_tui_turn_error_renders_as_error_bubble(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp

  async def fail(*args, **kwargs):
    raise RuntimeError("failed [status='down']")

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  runner = _MockRunner()
  monkeypatch.setattr(runner, 'send', fail)
  app = ChatApp(runner, None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._begin_turn()
    app._send_to_bro('trigger the failure')
    await app.workers.wait_for_complete()
    await pilot.pause()

    assert len(app.query(TypingIndicator)) == 0
    assert len(app.query('BubbleRow.bro')) == 1
    row = app.query_one('BubbleRow.error', BubbleRow)
    bubble = row.query_one('MessageBubble.error', MessageBubble)
    app.screen.selections = {bubble: SELECT_ALL}
    assert app.screen.get_selected_text() == "RuntimeError: failed [status='down']"
    assert bubble.styles.border_left[1] == bubble.styles.color
    assert bubble.styles.border_left[1] != app.query_one('MessageBubble.bro').styles.border_left[1]


@pytest.mark.asyncio
async def test_tui_answer_delivered_exits_carrying_the_answer(monkeypatch):
  from bro.launch.call_tui import ChatApp

  async def deliver(*args, **kwargs):
    raise AnswerDelivered('the verdict')

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  runner = _MockRunner()
  monkeypatch.setattr(runner, 'send', deliver)
  app = ChatApp(runner, None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._begin_turn()
    app._send_to_bro('finish it')
    await app.workers.wait_for_complete()
    await pilot.pause()
  assert app.delivered is not None
  assert app.delivered.answer == 'the verdict'


@pytest.mark.asyncio
async def test_tui_thinking_renders_as_muted_bubble_above_typing(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._begin_turn()
    assert app._observer is not None
    app._observer.on_event(TurnStartedEvent('work'))
    _consume_display(
      app,
      Reasoning(
        key='reasoning',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:34:56',
        content='**Planning**\n\nweighing the options',
      ),
    )
    await pilot.pause()
    row = app.query(BubbleRow).last()
    assert row.has_class('thinking')
    bubble = row.query_one(MessageBubble)
    # reasoning is plain authored text; Markdown parsing is reserved for assistant records
    app.screen.selections = {bubble: SELECT_ALL}
    assert app.screen.get_selected_text() == '**Planning**\n\nweighing the options'
    # lighter theme than a bro bubble: faded bar, muted text
    banner = app.query(MessageBubble).first()
    assert bubble.styles.border_left[1].a < banner.styles.border_left[1].a
    assert bubble.styles.color.a < banner.styles.color.a
    # mounted into the history stream above the typing indicator
    children = list(app.query_one('#history').children)
    assert children.index(row) < children.index(app.query_one(TypingIndicator))


@pytest.mark.asyncio
async def test_tui_mid_turn_message_renders_as_bro_bubble_above_typing(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    app._begin_turn()
    assert app._observer is not None
    app._observer.on_event(TurnStartedEvent('work'))
    app._observer.on_event(InterimAssistantTextEvent('on it — **rewriting** the tests'))
    await pilot.pause()
    row = app.query(BubbleRow).last()
    assert row.has_class('bro')
    bubble = row.query_one(MessageBubble)
    app.screen.selections = {bubble: SELECT_ALL}
    assert app.screen.get_selected_text() == 'on it — rewriting the tests'
    # the same theme a reply bubble gets, not the thinking one
    banner = app.query(MessageBubble).first()
    assert bubble.styles.border_left[1] == banner.styles.border_left[1]
    # the turn goes on below it: mounted above the typing indicator
    children = list(app.query_one('#history').children)
    assert children.index(row) < children.index(app.query_one(TypingIndicator))


@pytest.mark.asyncio
async def test_tui_timestamp_hugs_the_row_edge_with_seconds(monkeypatch):
  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test(size=(80, 40)) as pilot:
    _consume_display(
      app,
      UserInput(
        key='user',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:34:56',
        content='a message from the user',
      ),
    )
    _consume_display(
      app,
      AssistantText(
        key='reply',
        origin=Origin.SURFACE,
        timestamp='2026-05-28T12:35:07',
        content='a reply',
      ),
    )
    await pilot.pause()
    user_row = app.query_one('BubbleRow.user', BubbleRow)
    user_stamp = user_row.query_one('.timestamp')
    segments = [segment.text for segment in user_stamp.render_line(0)]
    assert ''.join(segments).strip() == '12:34:56'
    # the user bubble hugs the right edge and its timestamp hugs it too, below
    # the bubble — clear of the sender bar, which stops with the bubble
    user_bubble = user_row.query_one(MessageBubble)
    assert user_bubble.region.right == user_row.region.right
    assert user_stamp.region.right == user_row.region.right
    bro_row = app.query(BubbleRow).last()
    bro_stamp = bro_row.query_one('.timestamp')
    bro_bubble = bro_row.query_one(MessageBubble)
    # a bro-side timestamp starts in the column the sender bar occupies
    assert bro_bubble.region.x == bro_row.region.x
    assert bro_stamp.region.x == bro_row.region.x


@pytest.mark.asyncio
async def test_tui_copies_selection_to_clipboard_on_mouse_up(monkeypatch):
  from textual.events import TextSelected
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test() as pilot:
    # a plain click also posts TextSelected; with nothing selected the
    # clipboard must stay untouched
    app.screen.post_message(TextSelected())
    await pilot.pause()
    assert app.clipboard == ''
    banner_bubble = app.query_one(MessageBubble)
    app.screen.selections = {banner_bubble: SELECT_ALL}
    app.screen.post_message(TextSelected())
    await pilot.pause()
    # the banner bubble's copy text is the plain banner; the timestamp lives in
    # its own widget below and never rides along
    assert app.clipboard == 'BANNER'


@pytest.mark.asyncio
async def test_tui_shift_enter_breaks_line_and_enter_submits(monkeypatch):
  from textual.selection import SELECT_ALL

  from bro.launch.call_tui import ChatApp, MessageInput

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  runner = _MockRunner()
  app = ChatApp(runner, None)
  async with app.run_test() as pilot:
    field = app.query_one('#input-bar', MessageInput)
    assert field.region.height == 3
    await pilot.press('a', 'shift+enter', 'b')
    await pilot.pause()
    assert field.text == 'a\nb'
    # the field grows with its content
    assert field.region.height == 4
    await pilot.press('enter')
    await pilot.pause()
    assert field.text == ''
    bubble = app.query_one('BubbleRow.user', BubbleRow).query_one(MessageBubble)
    app.screen.selections = {bubble: SELECT_ALL}
    assert app.screen.get_selected_text() == 'a\nb'


@pytest.mark.asyncio
async def test_tui_enter_on_blank_input_submits_nothing(monkeypatch):
  from bro.launch.call_tui import ChatApp, MessageInput

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test() as pilot:
    await pilot.press('enter', 'shift+enter', 'enter')
    await pilot.pause()
    # nothing submitted, the field keeps its (blank) content
    assert len(app.query('BubbleRow.user')) == 0
    assert app.query_one('#input-bar', MessageInput).text == '\n'


def test_typing_status_text():

  assert _typing_status('thinking', 5) == 'Thinking for 5 seconds'
  assert _typing_status('thinking', 61) == 'Thinking for a minute'
  assert _typing_status('calling brog::create_task', 0.5) == 'Calling brog::create_task()'
  assert _typing_status('calling brog::create_task', 5) == 'Calling brog::create_task for 5 seconds'
  assert _typing_status('calling banner', 0.5) == 'Calling banner()'
  assert _typing_status('calling 3 tools', 5) == 'Calling 3 tools'


@pytest.mark.asyncio
async def test_tui_typing_indicator_tracks_run_state(monkeypatch):
  from textual.widgets import Static

  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  app = ChatApp(_MockRunner(), None)
  async with app.run_test() as pilot:
    app._begin_turn()
    assert app._observer is not None
    app._observer.on_event(TurnStartedEvent('work'))
    await pilot.pause()
    indicator = app.query_one(TypingIndicator)
    label = indicator.query_one(Static)
    assert str(label.content).startswith('Thinking for')
    app._observer.on_event(ToolCallEvent('call-1', 'web_search', {}))
    assert str(label.content).startswith('Calling web_search()')
    # a call running longer than a second gains its elapsed time
    indicator._phase_since -= 5
    indicator.tick()
    assert str(label.content).startswith('Calling web_search for 5 seconds')
    app._observer.on_event(ToolCallEvent('call-2', 'flow__list_tasks', {}))
    assert str(label.content).startswith('Calling 2 tools')
    app._observer.on_event(ToolResultEvent('call-1', 'web_search', 'done'))
    assert str(label.content).startswith('Calling flow::list_tasks()')
    app._observer.on_event(ToolResultEvent('call-2', 'flow__list_tasks', 'done'))
    # the batch's results are all in — the next LLM roundtrip starts, so the
    # thinking clock restarts from zero
    assert str(label.content).startswith('Thinking for a moment')


class _BlockingRunner(_MockRunner):
  """a runner whose turn never finishes on its own — the interruption fixture.

  `started` fires once `send` is running, and `cancelled` records that the
  cancellation reached the runner rather than only the worker wrapping it."""

  def __init__(self):
    super().__init__()
    self.started = asyncio.Event()
    self.messages: list[str] = []
    self.cancelled = False

  async def send(self, message, observer=None, tracker=None, request_timeout=None, **kwargs):
    self.messages.append(message)
    assert observer is not None
    observer.on_event(TurnStartedEvent(message))
    self.started.set()
    try:
      await asyncio.Event().wait()
    except asyncio.CancelledError:
      self.cancelled = True
      raise
    raise AssertionError('unreachable')


async def _start_turn(app, pilot, runner: '_BlockingRunner', text: str = 'work on it'):
  app._submit(text)
  await asyncio.wait_for(runner.started.wait(), timeout=5)
  await pilot.pause()


@pytest.mark.asyncio
async def test_tui_input_is_disabled_while_a_turn_runs(monkeypatch):
  from bro.launch.call_tui import _BUSY_PLACEHOLDER, _IDLE_PLACEHOLDER, ChatApp, MessageInput

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  runner = _BlockingRunner()
  app = ChatApp(runner, None)
  async with app.run_test(size=(80, 40)) as pilot:
    field = app.query_one('#input-bar', MessageInput)
    assert field.disabled is False
    await _start_turn(app, pilot, runner)
    assert field.disabled is True
    assert field.placeholder == _BUSY_PLACEHOLDER
    # a submit that reaches the app anyway (the field is the only way to raise
    # one, and it is disabled) never starts a second concurrent conversation
    await app.on_message_input_submitted(MessageInput.Submitted('second message'))
    await pilot.pause()
    assert runner.messages == ['work on it']

    await app.action_interrupt()
    await pilot.pause()
    assert field.disabled is False
    assert field.placeholder == _IDLE_PLACEHOLDER
    assert field.has_focus


@pytest.mark.asyncio
async def test_tui_escape_interrupts_the_turn(monkeypatch):
  from bro.launch.call_tui import (
    INTERRUPTED_NOTICE,
    ChatApp,
    MessageInput,
  )

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  runner = _BlockingRunner()
  app = ChatApp(runner, None)
  async with app.run_test(size=(80, 40)) as pilot:
    await _start_turn(app, pilot, runner)
    assert len(app.query(TypingIndicator)) == 1

    await pilot.press('escape')
    await pilot.pause()

    assert runner.cancelled, 'the cancellation must reach the runner, not just the worker'
    assert app._turn is None
    assert len(app.query(TypingIndicator)) == 0
    assert str(app.query(SystemBubble).last().content) == INTERRUPTED_NOTICE
    # the conversation continues: the next message is an ordinary turn
    await app.on_message_input_submitted(MessageInput.Submitted('never mind, do this'))
    await pilot.pause()
    assert app._turn is not None


@pytest.mark.asyncio
async def test_tui_quit_takes_the_running_turn_with_it(monkeypatch):
  from textual.worker import WorkerState

  from bro.launch.call_tui import ChatApp

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')
  runner = _BlockingRunner()
  app = ChatApp(runner, None)
  async with app.run_test(size=(80, 40)) as pilot:
    await _start_turn(app, pilot, runner)
    worker = app._turn
    assert worker is not None

    await app.action_quit()

    assert worker.state is WorkerState.CANCELLED
    assert runner.cancelled


@pytest.mark.asyncio
async def test_text_mode_ctrl_c_ends_the_turn_not_the_chat(capsys, monkeypatch):
  from bro.launch.call import INTERRUPTED_NOTICE

  monkeypatch.setattr('bro.workspace.banner.render_banner', lambda llm=False, bro=None: 'BANNER')

  class _InterruptedRunner(_MockRunner):
    def __init__(self):
      super().__init__()
      self.cancelled = False

    async def send(self, message, observer=None, tracker=None, request_timeout=None, **kwargs):
      assert observer is not None
      observer.on_event(TurnStartedEvent(message))
      if message != 'work on it':
        observer.on_event(TurnCompletedEvent('second reply'))
        return 'second reply'
      # Ctrl+C as the terminal delivers it, while the turn's handler is installed
      signal.raise_signal(signal.SIGINT)
      try:
        await asyncio.Event().wait()
      except asyncio.CancelledError:
        self.cancelled = True
        raise
      raise AssertionError('unreachable')

  runner = _InterruptedRunner()
  await call_text(
    runner,
    'work on it',
    read_line=_ScriptedLines(['and now this']),
    now=_fixed_now,
    hold='attended',
  )

  assert runner.cancelled
  out = capsys.readouterr().out
  assert INTERRUPTED_NOTICE in out
  # the REPL kept going: the next message is an ordinary exchange
  assert 'record: second reply' in out
  # and SIGINT is back to ending the chat once no turn is running
  assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_managed_continuation_uses_the_recorded_recipe_and_hold(monkeypatch):
  from bro.launch.resume import ResumedCall

  captured: dict = {}
  resumed_runner = _MockRunner()
  recorded_spec = llm_llms_openai.LLMSpec(model='gpt-5', reasoning_effort='low')

  def fake_resume(client, bro_name, trail_ref, *, llm_spec, at=None, hold='guided'):
    captured.update(trail_ref=trail_ref, llm_spec=llm_spec, at=at, hold=hold)
    return ResumedCall(runner=resumed_runner, history=[], trail_id=trail_ref)

  async def fake_call_text(runner, initial, history=None, hold='guided', preset_name=None):
    captured.update(runner=runner, initial=initial, history=history, chat_hold=hold)

  monkeypatch.setenv('RIDE_IN_CONTAINER', '1')
  monkeypatch.setattr('bro.launch.resume.resume', fake_resume)
  monkeypatch.setattr('bro.trails.store.default_store', lambda: MagicMock())
  monkeypatch.setattr('bro.launch.call.call_text', fake_call_text)
  monkeypatch.setattr('bro.launch.call._tty_supported', lambda: False)

  code = chat_main(
    [
      'bro',
      'record',
      '--continue-trail',
      'workspace-trail',
      '--continue-llm',
      json.dumps(recorded_spec.dump()),
      '--hold',
      'attended',
    ],
    program=['bro', 'chat'],
  )
  assert code is None
  assert captured['trail_ref'] == 'workspace-trail'
  assert captured['llm_spec'] == recorded_spec
  assert captured['hold'] == 'attended'
  assert captured['runner'] is resumed_runner
  assert captured['initial'] is None
  assert captured['history'] == []
  assert captured['chat_hold'] == 'attended'
