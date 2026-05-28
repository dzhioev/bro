from datetime import datetime

import pytest

from bro.bro import BaseBro
from do.call import call_raw
from llm.llm import LLM
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


class RecordBro(BaseBro):
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
async def test_raw_drives_send_until_eof(capsys):
  bro = RecordBro(response='reply')
  await call_raw(bro, 'first', read_line=_ScriptedLines(['second', 'third']), now=_fixed_now)
  assert len(bro.mock_llm.send_calls) == 3
  assert bro.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'first'}
  assert bro.mock_llm.send_calls[1] == [{'role': 'user', 'content': 'second'}]
  assert bro.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'third'}]
  out = capsys.readouterr().out
  # each reply line is `[HH:MM:SS] <bro-name>: <reply>`
  assert out.count('[12:34:56] record: reply') == 3


@pytest.mark.asyncio
async def test_raw_skips_empty_input(capsys):
  bro = RecordBro(response='reply')
  await call_raw(bro, 'first', read_line=_ScriptedLines(['', '   ', 'real']), now=_fixed_now)
  # empty string skipped; whitespace-only is sent through (boundary belongs upstream)
  assert len(bro.mock_llm.send_calls) == 3  # first + '   ' + 'real'
  assert bro.mock_llm.send_calls[1] == [{'role': 'user', 'content': '   '}]
  assert bro.mock_llm.send_calls[2] == [{'role': 'user', 'content': 'real'}]


@pytest.mark.asyncio
async def test_raw_returns_on_immediate_eof(capsys):
  bro = RecordBro(response='reply')
  await call_raw(bro, 'only', read_line=_ScriptedLines([]), now=_fixed_now)
  assert len(bro.mock_llm.send_calls) == 1
  assert bro.mock_llm.send_calls[0][-1] == {'role': 'user', 'content': 'only'}
