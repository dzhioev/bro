import os
from typing import Optional
from unittest.mock import patch

import pytest

from bro.bro import BaseBro
from do.do_task import do_task, main
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

  def __init__(self, response: str = 'done'):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)

  def _make_observer(self) -> Observer:
    return NullObserver()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.mock_llm


@pytest.mark.asyncio
async def test_wraps_raw_ref_as_fix_invocation():
  bro = RecordBro(response='fixed')
  result = await do_task(bro, 'abc-123')
  assert result == 'fixed'
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1] == {'role': 'user', 'content': '/fix abc-123'}


@pytest.mark.asyncio
async def test_wraps_url_as_fix_invocation():
  bro = RecordBro(response='ok')
  url = 'https://www.notion.so/foo/add-X-369d38d85a6d818caf91c12a203b17e1?source=copy_link'
  await do_task(bro, url)
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == f'/fix {url}'


@pytest.mark.asyncio
async def test_passes_through_leading_slash_invocation():
  # `do-task ppp-dev "/fix --new idea"` must not become `/fix /fix --new idea`,
  # and an alternate skill (`/pr …`) is a deliberate override left untouched.
  bro = RecordBro(response='ok')
  await do_task(bro, '/fix --new idea')
  await do_task(bro, '/pr')
  assert bro.mock_llm.send_calls[0][-1]['content'] == '/fix --new idea'
  assert bro.mock_llm.send_calls[1][-1] == {'role': 'user', 'content': '/pr'}


@pytest.mark.asyncio
async def test_leading_whitespace_is_wrapped_not_passed_through():
  # the pass-through rule keys on the first character, so a leading-whitespace
  # input is wrapped like any other ref rather than treated as an invocation.
  bro = RecordBro(response='ok')
  await do_task(bro, '  /pr')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == '/fix   /pr'


def test_main_re_execs_into_container_when_outside():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('PPP_SHELL_COMMAND', None)
    rc = main(['do-task', 'ppp-dev', 'abc-123'])
    assert rc == 0
    assert env['PPP_SHELL_COMMAND'] == 'do-task ppp-dev abc-123'
    (workspace, command), kwargs = run.call_args
    assert workspace.startswith('do-task-ppp-dev-')
    assert command == ['do-task', 'ppp-dev', 'abc-123', '--host']
    assert kwargs['drop'] is True
    assert {'github', 'brog', 'trails'} <= kwargs['secrets']
    assert kwargs['docker_sock'] is False


def test_main_relay_wraps_the_task_as_a_fix_invocation():
  # a relayed child runs plain `ask`, so the `/fix` wrapping happens before the
  # summon request is sent.
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1', 'BROKER_CHANNEL': 'unix:/tmp/x.sock'}),
    patch('summon.relay_summon', return_value=0) as relay,
  ):
    rc = main(['do-task', 'ppp-dev', 'abc-123'])
  assert rc == 0
  relay.assert_called_once_with('ppp-dev', '/fix abc-123', timeout=None, into=None)


def test_main_exports_task_id_before_the_hop():
  # the hop forwards CW_TASK_ID from this process's environment at container
  # create time, so the export must precede run_in_container.
  seen_at_hop: dict[str, Optional[str]] = {}

  def record_env(*args, **kwargs):
    seen_at_hop['task_id'] = os.environ.get('CW_TASK_ID')
    return 0

  url = 'https://www.notion.so/foo/add-X-369d38d85a6d818caf91c12a203b17e1?source=copy_link'
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', side_effect=record_env),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('CW_TASK_ID', None)
    rc = main(['do-task', 'ppp-dev', url])
  assert rc == 0
  assert seen_at_hop['task_id'] == '369d38d8-5a6d-818c-af91-c12a203b17e1'


def test_main_exports_task_id_for_dashed_uuid():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('CW_TASK_ID', None)
    rc = main(['do-task', 'ppp-dev', '369d38d8-5a6d-818c-af91-c12a203b17e1'])
    assert rc == 0
    assert os.environ.get('CW_TASK_ID') == '369d38d8-5a6d-818c-af91-c12a203b17e1'


@pytest.mark.parametrize('task', ['fix the frobnicator', '/fix --new idea'])
def test_main_exports_nothing_for_non_page_ref_input(task: str):
  # a description or slash invocation names no page — the bro resolves the task
  # itself, and any ambient CW_TASK_ID is left as-is.
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('CW_TASK_ID', None)
    rc = main(['do-task', 'ppp-dev', task])
    assert rc == 0
    assert os.environ.get('CW_TASK_ID') is None
