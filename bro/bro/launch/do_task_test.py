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

  def __init__(self, response: str = 'done', skills: dict[str, str] | None = None):
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)
    self._fake_skills = skills if skills is not None else {'fix': 'FIX BODY'}

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
async def test_wraps_raw_ref_in_fix_skill():
  bro = RecordBro(response='fixed')
  result = await do_task(bro, 'abc-123')
  assert result == 'fixed'
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1] == {'role': 'user', 'content': 'FIX BODY\n\nARGUMENTS: abc-123'}


@pytest.mark.asyncio
async def test_wraps_url_in_fix_skill():
  bro = RecordBro(response='ok')
  url = 'https://www.notion.so/foo/add-X-369d38d85a6d818caf91c12a203b17e1?source=copy_link'
  await do_task(bro, url)
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == f'FIX BODY\n\nARGUMENTS: {url}'


@pytest.mark.asyncio
async def test_passes_through_leading_slash_invocation():
  # `do-task ppp-dev "/fix --focus"` must not become `/fix /fix --focus`.
  bro = RecordBro(response='ok')
  await do_task(bro, '/fix --focus')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == 'FIX BODY\n\nARGUMENTS: --focus'


@pytest.mark.asyncio
async def test_passes_through_alternate_skill_invocation():
  bro = RecordBro(response='ok', skills={'pr': 'PR BODY', 'fix': 'FIX BODY'})
  await do_task(bro, '/pr')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == 'PR BODY'


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
