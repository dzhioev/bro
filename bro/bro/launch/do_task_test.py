from unittest.mock import patch

import pytest

from bro.bro import BaseBro
from do.do_task import do_task, main
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
async def test_wraps_task_reference_in_fix_prompt():
  bro = RecordBro(response='fixed')
  result = await do_task(bro, 'abc-123')
  assert result == 'fixed'
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1] == {'role': 'user', 'content': 'fix the flow task: abc-123'}


@pytest.mark.asyncio
async def test_passes_arbitrary_string_through():
  bro = RecordBro(response='ok')
  url = 'https://www.notion.so/foo/add-X-to-media-369d38d85a6d818caf91c12a203b17e1?source=copy_link'
  await do_task(bro, url)
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == f'fix the flow task: {url}'


def test_main_re_execs_into_container_when_outside():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    rc = main(['do-task', 'ppp-dev', 'abc-123'])
    assert rc == 0
    (workspace, command), kwargs = run.call_args
    assert workspace.startswith('do-task-ppp-dev-')
    assert command == ['do-task', 'ppp-dev', 'abc-123']
    assert kwargs == {'drop': True}
