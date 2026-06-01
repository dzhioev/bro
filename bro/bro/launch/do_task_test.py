from pathlib import Path
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
    # set _fake_skills before super().__init__() — BaseBro's init reads
    # self.skills (which we override below) when building the service server.
    self._fake_skills = skills if skills is not None else {'fix': 'FIX BODY'}
    super().__init__(system_prompt='record')
    self.mock_llm = MockLLM(response=response)

  def _make_tracer(self) -> Tracer:
    return NullTracer()

  def _create_llm(self, *, interactive: bool) -> LLM:
    return self.mock_llm

  @property
  def skills(self) -> dict[str, Path]:
    # mirror _fake_skills so the membership check `'fix' in bro.skills` in
    # do_task() sees what get_skill_body would actually serve. The Path values
    # are never read because we override skill_descriptions + get_skill_body.
    return {name: Path(f'fake/{name}.md') for name in self._fake_skills}

  def skill_descriptions(self) -> list[tuple[str, str]]:
    # override the FS-reading default; BaseBro.__init__ calls this so the
    # fake paths above must never be opened.
    return [(name, '') for name in self._fake_skills]

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


@pytest.mark.asyncio
async def test_raises_helpful_error_when_bro_has_no_fix():
  # `do-task` only makes sense for bros that expose `/fix`; surface a usable
  # hint instead of the generic "no skill named 'fix'" KeyError.
  bro = RecordBro(skills={'pr': 'PR BODY'})
  with pytest.raises(KeyError) as exc:
    await do_task(bro, 'some-task-ref')
  msg = str(exc.value.args[0])
  assert "'fix'" in msg
  assert "'record'" in msg
  assert "'ask'" in msg


@pytest.mark.asyncio
async def test_no_fix_check_skipped_for_explicit_slash_override():
  # `do-task ppp-dev "/pr"` lets the user pick a different skill — the /fix
  # pre-flight only applies when we'd be wrapping a raw ref.
  bro = RecordBro(response='ok', skills={'pr': 'PR BODY'})
  await do_task(bro, '/pr')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'] == 'PR BODY'


@pytest.mark.asyncio
async def test_leading_whitespace_is_not_slash_prefixed():
  # do_task no longer lstrips before the slash check — aligned with do.py's
  # _SKILL_INVOCATION regex which also rejects leading whitespace. A
  # leading-whitespace input is wrapped to /fix '<input>' rather than passing
  # through as already-slash-prefixed.
  bro = RecordBro(response='ok')
  await do_task(bro, '  /pr')
  messages = bro.mock_llm.send_calls[0]
  assert messages[-1]['content'].startswith('FIX BODY\n\nARGUMENTS:')
  assert '/pr' in messages[-1]['content']


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
