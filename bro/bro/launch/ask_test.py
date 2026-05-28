from unittest.mock import patch

import pytest

from bro.bro import BaseBro
from do.do import do, main
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

  def __init__(self, response: str = 'done'):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.mock_llm


@pytest.mark.asyncio
async def test_forwards_to_bro_run():
  bro = RecordBro(response='ok')
  result = await do(bro, 'hello')
  assert result == 'ok'
  assert len(bro.mock_llm.send_calls) == 1
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1] == {'role': 'user', 'content': 'hello'}


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
