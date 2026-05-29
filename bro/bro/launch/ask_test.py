from unittest.mock import patch

import pytest

from bro.bro import BaseBro
from do.do import _expand_skill_invocation, do, main
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

  def __init__(self, response: str = 'done', skills: dict[str, str] | None = None):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)
    self._fake_skills = skills if skills is not None else {}

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.mock_llm

  def get_skill_body(self, name: str) -> str:
    if name in self._fake_skills:
      return self._fake_skills[name]
    available = ', '.join(sorted(self._fake_skills)) if len(self._fake_skills) > 0 else '(none)'
    raise KeyError(f'no skill named {name!r}; available: {available}')


@pytest.mark.asyncio
async def test_forwards_to_bro_run():
  bro = RecordBro(response='ok')
  result = await do(bro, 'hello')
  assert result == 'ok'
  assert len(bro.mock_llm.send_calls) == 1
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1] == {'role': 'user', 'content': 'hello'}


@pytest.mark.asyncio
async def test_slash_skill_expands_with_arguments():
  bro = RecordBro(response='ok', skills={'fix': 'FIX BODY'})
  await do(bro, '/fix https://example.com/x')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == 'FIX BODY\n\nARGUMENTS: https://example.com/x'


@pytest.mark.asyncio
async def test_slash_skill_without_arguments_omits_arguments_line():
  bro = RecordBro(response='ok', skills={'fix': 'FIX BODY'})
  await do(bro, '/fix')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == 'FIX BODY'


def test_expand_passes_non_slash_input_through():
  bro = RecordBro(skills={'fix': 'FIX BODY'})
  assert _expand_skill_invocation(bro, 'plain text') == 'plain text'
  assert _expand_skill_invocation(bro, '  / leading space is not a skill') == (
    '  / leading space is not a skill'
  )
  assert _expand_skill_invocation(bro, '// double slash') == '// double slash'


def test_expand_unknown_skill_raises_key_error():
  bro = RecordBro(skills={'fix': 'FIX BODY'})
  with pytest.raises(KeyError) as exc:
    _expand_skill_invocation(bro, '/nope something')
  msg = str(exc.value)
  assert 'nope' in msg
  assert 'fix' in msg


def test_expand_multiline_arguments_preserved():
  bro = RecordBro(skills={'fix': 'FIX BODY'})
  expanded = _expand_skill_invocation(bro, '/fix line one\nline two\nline three')
  assert expanded == 'FIX BODY\n\nARGUMENTS: line one\nline two\nline three'


def test_main_re_execs_into_container_when_outside():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello world', '--rich'])
    assert rc == 0
    assert run.call_count == 1
    (workspace, command), kwargs = run.call_args
    assert workspace.startswith('ask-ppp-dev-')
    assert command == ['ask', 'ppp-dev', 'hello world', '--rich']
    assert kwargs == {'drop': True}


def test_main_skips_container_when_inside():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('cw.run_in_container') as run,
    patch('bro.registry.get_bro', return_value=RecordBro(response='ok')),
  ):
    rc = main(['ask', 'record', 'hi'])
    assert rc is None
    assert run.call_count == 0


def test_main_skips_container_with_no_container_flag():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
    patch('bro.registry.get_bro', return_value=RecordBro(response='ok')),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'record', 'hi', '--no-container'])
    assert rc is None
    assert run.call_count == 0


def test_main_surfaces_unknown_skill_as_stderr_exit_1(capsys):
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('bro.registry.get_bro', return_value=RecordBro(skills={'fix': 'FIX BODY'})),
  ):
    rc = main(['ask', 'record', '/nope something'])
  assert rc == 1
  captured = capsys.readouterr()
  assert 'nope' in captured.err
  assert 'fix' in captured.err
