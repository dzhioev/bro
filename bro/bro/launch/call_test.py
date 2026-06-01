import io
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

import pytest

import llm.llms.chat_gpt
from bro.bros.bro import Bro
from do.call import TextTracer, call_text, main
from llm.llm import LLM, LLMSpec
from llm.mcp import MCPServer
from llm.tracer import NullTracer, Tracer


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: list[MCPServer] | None = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict]) -> str:
    self.send_calls.append(messages)
    return self.response


class RecordBro(Bro):
  name = 'record'
  description = 'records inputs'

  def __init__(self, response: str = 'reply'):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
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


@pytest.mark.asyncio
async def test_text_drives_send_until_eof(capsys):
  bro = RecordBro(response='reply')
  await call_text(
    bro,
    'first',
    tracer=NullTracer(),
    read_line=_ScriptedLines(['second', 'third']),
    now=_fixed_now,
  )
  assert len(bro.mock_llm.send_calls) == 3
  assert bro.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'first'}
  assert bro.mock_llm.send_calls[1] == [{'role': 'user', 'content': 'second'}]
  assert bro.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'third'}]
  out = capsys.readouterr().out
  # each reply line is `[HH:MM:SS] <bro-name>: <reply>`
  assert out.count('[12:34:56] record: reply') == 3


@pytest.mark.asyncio
async def test_text_skips_empty_input(capsys):
  bro = RecordBro(response='reply')
  await call_text(
    bro,
    'first',
    tracer=NullTracer(),
    read_line=_ScriptedLines(['', '   ', 'real']),
    now=_fixed_now,
  )
  # empty string skipped; whitespace-only is sent through (boundary belongs upstream)
  assert len(bro.mock_llm.send_calls) == 3  # first + '   ' + 'real'
  assert bro.mock_llm.send_calls[1] == [{'role': 'user', 'content': '   '}]
  assert bro.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'real'}]


@pytest.mark.asyncio
async def test_text_returns_on_immediate_eof(capsys):
  bro = RecordBro(response='reply')
  await call_text(bro, 'only', tracer=NullTracer(), read_line=_ScriptedLines([]), now=_fixed_now)
  assert len(bro.mock_llm.send_calls) == 1
  assert bro.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'only'}


def _make_text_tracer() -> tuple[TextTracer, io.StringIO]:
  out = io.StringIO()
  tracer = TextTracer(prefix='bro', file=out, now=lambda: '12:34:56')
  return tracer, out


def test_text_tracer_renders_reasoning_one_liner():
  tracer, out = _make_text_tracer()
  tracer.on_reasoning('user wants a movie rec\nwithout horror, please')
  assert (
    out.getvalue() == '[12:34:56] bro · thinking: user wants a movie rec without horror, please\n'
  )


def test_text_tracer_renders_tool_call_with_compact_json():
  tracer, out = _make_text_tracer()
  tracer.on_tool_call('web_search', {'query': 'sci-fi movies'})
  assert out.getvalue() == '[12:34:56] bro → web_search {"query":"sci-fi movies"}\n'


def test_text_tracer_renders_tool_result_compacts_json_string():
  tracer, out = _make_text_tracer()
  tracer.on_tool_result('web_search', '[\n  "Arrival",\n  "Annihilation"\n]')
  assert out.getvalue() == '[12:34:56] bro ← web_search ["Arrival","Annihilation"]\n'


def test_text_tracer_truncates_long_interim_message():
  tracer, out = _make_text_tracer()
  tracer.on_assistant_message('x' * 500, terminal=False)
  # body capped at _MESSAGE_LIMIT (240); the line ends with the overflow marker
  assert '<260 more chars>' in out.getvalue()


def test_text_tracer_skips_terminal_message():
  tracer, out = _make_text_tracer()
  tracer.on_assistant_message('here is the answer', terminal=True)
  # call_text renders the reply itself; the tracer must not double-emit
  assert out.getvalue() == ''


@dataclass(frozen=True)
class _FastlessSpec(LLMSpec):
  """test spec that intentionally has no fast-mode equivalent."""

  TYPE: ClassVar[str] = 'fastless'

  model: str = 'whatever'

  def create_llm(self, mcp_servers=None, tracer=None) -> LLM:
    raise NotImplementedError('not constructible in tests')

  def dump(self) -> dict:
    return {'type': self.TYPE, 'model': self.model}

  @classmethod
  def _from_dict_impl(cls, data: dict) -> 'LLMSpec':
    return cls(model=data['model'])


class _ChatBro(Bro):
  name = 'record'
  description = 'records inputs'
  llm_spec = llm.llms.chat_gpt.LLMSpec(model='gpt-5.4-mini')

  def __init__(self):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response='reply')

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.mock_llm


class _FastlessBro(Bro):
  name = 'fastless'
  description = 'has a spec without fast support'
  llm_spec = _FastlessSpec(model='whatever')

  def __init__(self):
    super().__init__(system_prompt='fastless')
    self.mock_llm = MockLLM(response='reply')

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.mock_llm


def test_fast_flag_invokes_spec_fast(monkeypatch):
  built: list[Bro] = []

  async def fake_call_text(bro, initial):
    built.append(bro)

  monkeypatch.setattr('bro.registry.get_class', lambda name: _ChatBro)
  monkeypatch.setattr('do.call.call_text', fake_call_text)
  monkeypatch.setattr('do.call._tty_supported', lambda: False)

  rc = main(['call', 'record', 'hi', '--fast'])
  assert rc is None
  assert len(built) == 1
  spec = built[0].llm_spec
  assert isinstance(spec, llm.llms.chat_gpt.LLMSpec)
  assert spec.service_tier == 'priority'
  # class default untouched — fast() returns a fresh spec
  default = _ChatBro.llm_spec
  assert isinstance(default, llm.llms.chat_gpt.LLMSpec)
  assert default.service_tier is None


def test_fast_flag_reports_when_bro_spec_has_no_fast_mode(monkeypatch, capsys):
  monkeypatch.setattr('bro.registry.get_class', lambda name: _FastlessBro)
  monkeypatch.setattr('do.call._tty_supported', lambda: False)

  rc = main(['call', 'fastless', 'hi', '--fast'])
  assert rc == 1
  err = capsys.readouterr().err
  assert '--fast' in err
  assert '_FastlessSpec' in err


class _FakeApp:
  """captures `append_trace_line` calls; stands in for `ChatApp` in TUITracer
  tests so we don't have to spin up a Textual runtime."""

  def __init__(self):
    self.posted: list[str] = []

  def call_from_thread(self, fn, *args, **kwargs):
    fn(*args, **kwargs)

  def append_trace_line(self, text: str) -> None:
    self.posted.append(text)


def test_tui_tracer_posts_one_line_per_event():
  from do.call_tui import TUITracer

  app = _FakeApp()
  tracer = TUITracer(app)  # type: ignore[arg-type]
  tracer.on_reasoning('user wants\na movie rec')
  tracer.on_tool_call('web_search', {'query': 'sci-fi'})
  tracer.on_tool_result('web_search', '[\n  "Arrival"\n]')
  tracer.on_assistant_message('thinking out loud', terminal=False)
  tracer.on_assistant_message('the final answer', terminal=True)

  assert app.posted == [
    '✎ thinking: user wants a movie rec',
    '→ web_search {"query":"sci-fi"}',
    '← web_search ["Arrival"]',
    '✎ says: thinking out loud',
    # terminal message is skipped — ChatApp renders the reply itself
  ]
