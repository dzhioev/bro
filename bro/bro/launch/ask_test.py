from typing import Optional
from unittest.mock import patch

import pytest

from bro.bro import BaseBro
from do.do import do, expand_skill_invocation, main
from llm.llm import LLM
from llm.mcp import MCPServer
from llm.observer import NullObserver, Observer


class MockLLM(LLM):
  def __init__(self, response: str = 'mock', mcp_servers: Optional[list[MCPServer]] = None):
    super().__init__(mcp_servers)
    self.response = response
    self.send_calls: list[list[dict]] = []

  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    self.send_calls.append(messages)
    return self.response


class RecordBro(BaseBro):
  name = 'record'
  description = 'records inputs'

  def __init__(self, response: str = 'done', skills: Optional[dict[str, str]] = None):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)
    self._fake_skills = skills if skills is not None else {}

  def _make_observer(self) -> Observer:
    return NullObserver()

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
  assert expand_skill_invocation(bro, 'plain text') == 'plain text'
  assert expand_skill_invocation(bro, '  / leading space is not a skill') == (
    '  / leading space is not a skill'
  )
  assert expand_skill_invocation(bro, '// double slash') == '// double slash'


def test_expand_unknown_skill_raises_key_error():
  bro = RecordBro(skills={'fix': 'FIX BODY'})
  with pytest.raises(KeyError) as exception:
    expand_skill_invocation(bro, '/nope something')
  msg = str(exception.value)
  assert 'nope' in msg
  assert 'fix' in msg


def test_expand_multiline_arguments_preserved():
  bro = RecordBro(skills={'fix': 'FIX BODY'})
  expanded = expand_skill_invocation(bro, '/fix line one\nline two\nline three')
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
    assert kwargs['drop'] is True
    # ppp-dev's manifest (github + notion via flow) plus the mandatory trails sink
    assert {'github', 'notion', 'trails'} <= kwargs['secrets']
    # ppp-dev doesn't deploy → no docker socket
    assert kwargs['docker_sock'] is False


def test_main_default_forwards_no_slow_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello'])
    assert rc == 0
    (_workspace, command), _kwargs = run.call_args
    # fast is the default, so nothing extra is forwarded; the in-container run applies fast()
    assert command == ['ask', 'ppp-dev', 'hello']


def test_main_forwards_slow_into_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello', '--slow'])
    assert rc == 0
    (_workspace, command), _kwargs = run.call_args
    # --slow is forwarded like --rich; the in-container run builds the plain spec
    assert command == ['ask', 'ppp-dev', 'hello', '--slow']


def test_main_no_trails_disables_recording_in_container():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'ppp-dev', 'hello', '--no-trails'])
    assert rc == 0
    (_workspace, command), kwargs = run.call_args
    # the env var carries the effect in, so --no-trails isn't forwarded into the inner argv
    assert command == ['ask', 'ppp-dev', 'hello']
    assert 'trails' not in kwargs['secrets']
    assert kwargs['extra_env'] == {'TRAILS_DISABLED': '1'}


def test_main_no_trails_with_host_is_an_error():
  with pytest.raises(SystemExit):
    main(['ask', 'ppp-dev', 'hello', '--no-trails', '--host'])


def test_main_skips_container_when_inside():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('cw.run_in_container') as run,
    patch('bro.registry.create_bro', return_value=RecordBro(response='ok')),
  ):
    # --slow routes through the patched create_bro (the plain spec path); the
    # container-skip behavior under test is independent of fast/slow.
    rc = main(['ask', 'record', 'hi', '--slow'])
    assert rc is None
    assert run.call_count == 0


def test_main_skips_container_with_host_flag():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container') as run,
    patch('bro.registry.create_bro', return_value=RecordBro(response='ok')),
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['ask', 'record', 'hi', '--host', '--slow'])
    assert rc is None
    assert run.call_count == 0


def test_main_surfaces_unknown_skill_as_stderr_exit_1(capsys):
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1'}),
    patch('bro.registry.create_bro', return_value=RecordBro(skills={'fix': 'FIX BODY'})),
  ):
    rc = main(['ask', 'record', '/nope something', '--slow'])
  assert rc == 1
  captured = capsys.readouterr()
  assert 'nope' in captured.err
  assert 'fix' in captured.err
