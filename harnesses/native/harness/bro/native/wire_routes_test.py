import asyncio
import dataclasses
import json
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

from bro.base import credentials
from bro.fork import fork, latest_fork_point
from bro.launch.resume import resume
from bro.llm import retry as llm_retry
from bro.llm.llm import LLMStopped
from bro.llm.mcp import FunctionTool, InProcessMCPServer, ToolRegistry
from bro.llm.wire import Leg, SystemPrompt, Text, UserMessage, Wire
from bro.native import loop as native_loop, runner as native_runner
from bro.native.echo_declaration import LLMSpec as EchoSpec
from bro.native.runner import Runner
from bro.native.silent_turn_test_helper import JOB_TOOL, ReleaseWhenIdle, ShellBro, job_arguments
from bro.native.wire_fixtures_test_helper import call, message, response
from bro.openai.declaration import LLMSpec as ResponsesSpec
from bro.registry import create_bro
from bro.trails.local import LocalStore
from bro.trails.record.bro import Recorder
from bro.trails.store import fetch_recorded_trail
from bro.wire_chat.decode import counts, decode


@pytest.fixture
def replies():
  return []


@pytest.fixture
def server(monkeypatch, replies):
  requests: list[dict] = []

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      assert self.path == '/v1/responses'
      request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
      requests.append(request)
      raw = (
        replies.pop(0) if replies else response(message('reply'), id=f'response_{len(requests)}')
      )
      payload = json.dumps(raw).encode()
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, format: str, *args):
      pass

  with ExitStack() as resources:
    server = resources.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Handler))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    resources.callback(thread.join)
    resources.callback(server.shutdown)
    monkeypatch.setenv('OPENAI_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setattr(credentials, 'get_json', lambda kind: {'api_key': 'fixture-key'})
    yield requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'recipe', [EchoSpec(), ResponsesSpec(model='gpt-6.1-sol')], ids=['echo', 'responses']
)
async def test_run_record_resume_and_fork(recipe, server, monkeypatch, tmp_path):
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(create_bro('bro', llm_spec=recipe))
  assert await runner.run('reply', surface='test') == 'reply'
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  point = latest_fork_point(recorded)
  calls = [step for step in recorded.steps if step.kind == 'llm_call']
  assert [step.extras['wire'] for step in calls] == [recipe.WIRE]
  assert any(event['type'] == 'assistant' for event in store.iter_messages(recorded.header.id))

  resumed = resume(store, 'bro', recorded.header.id, llm_spec=recipe, hold='unattended')
  with resumed.runner:
    assert await resumed.runner.send('reply', surface='test') == 'reply'
  assert resumed.runner.trail_id is not None
  child = fetch_recorded_trail(store, resumed.runner.trail_id)
  assert child.header.forked_from is not None
  assert child.header.forked_from.trail_id == recorded.header.id
  if server:
    assert server[-1]['previous_response_id'] == calls[-1].extras['response_id']
    assert server[-1]['input'] == [{'role': 'user', 'content': 'reply'}]

  restored = fork(
    recorded, point, system_prompt='different prompt', hold='unattended', surface='test'
  )
  with restored:
    assert await restored.send('reply', surface='test') == 'reply'
  if server:
    assert 'previous_response_id' not in server[-1]
    assert server[-1]['input'][0] == {'role': 'system', 'content': 'different prompt'}
    assert any(item.get('role') == 'assistant' for item in server[-1]['input'])

  # Untagged persisted records must continue through both chain and replay paths.
  if calls[-1].extras.get('response_id') is not None:
    legacy = dataclasses.replace(
      recorded,
      steps=[
        dataclasses.replace(
          step, extras={key: value for key, value in step.extras.items() if key != 'wire'}
        )
        for step in recorded.steps
      ],
    )
    for prompt in (None, 'legacy replay'):
      continuation = fork(
        legacy, point, system_prompt=prompt, hold='unattended', record=False, surface='test'
      )
      with continuation:
        assert await continuation.send('reply', surface='test') == 'reply'
      assert ('previous_response_id' in server[-1]) == (prompt is None)


@pytest.mark.asyncio
async def test_stopped_run_records_failure_usage_and_resumes_before_refused_call(
  server, replies, monkeypatch, tmp_path
):
  stopped = response(message('partial'), id='refused')
  stopped.update(
    status='incomplete',
    incomplete_details={'reason': 'max_output_tokens'},
    usage={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12},
  )
  replies.append(stopped)
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(create_bro('bro', llm_spec=ResponsesSpec(model='gpt-6.1-sol')))
  with pytest.raises(LLMStopped, match='max_output_tokens'):
    await runner.run('original', surface='test')
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  header = store.get_trail(recorded.header.id)
  assert header['end']['reason'] == 'error'
  assert 'max_output_tokens' in header['end']['detail']
  calls = [step for step in recorded.steps if step.kind == 'llm_call']
  assert calls[0].extras['stopped'] == 'truncated'
  assert recorded.header.native['usage']
  projected = list(store.iter_messages(recorded.header.id))
  assert next(event for event in projected if event['type'] == 'llm_call')['stop'] == 'truncated'
  resumed = resume(
    store,
    'bro',
    recorded.header.id,
    llm_spec=ResponsesSpec(model='gpt-6.1-sol'),
    hold='unattended',
  )
  with resumed.runner:
    assert await resumed.runner.send('continue', surface='test') == 'reply'
  assert 'previous_response_id' not in server[-1]
  assert not any(item.get('role') == 'assistant' for item in server[-1]['input'])
  assert [item['content'] for item in server[-1]['input'] if item.get('role') == 'user'] == [
    'original',
    'continue',
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize('refusals, expected_wait', [(3, 70), (100, 610)])
async def test_run_capacity_attempts_are_all_recorded_and_counted(
  refusals, expected_wait, monkeypatch, tmp_path
):
  waits = []
  requests = []
  wire = MagicMock(spec=Wire)

  async def pause(seconds):
    waits.append(seconds)

  async def exchange(inputs, tools, request_timeout):
    requests.append(list(inputs))
    stopped = len(requests) <= refusals
    raw = {
      'model': 'gpt-6.1-sol',
      'choices': [
        {
          'finish_reason': 'insufficient_system_resource' if stopped else 'stop',
          'message': {'content': 'partial' if stopped else 'reply'},
        }
      ],
      'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
    }
    items = []
    for item in inputs:
      assert isinstance(item, (SystemPrompt, UserMessage))
      if isinstance(item, SystemPrompt):
        text = item.text
      else:
        assert len(item.content) == 1 and isinstance(item.content[0], Text)
        text = item.content[0].text
      items.append(
        {
          'role': 'system' if isinstance(item, SystemPrompt) else 'user',
          'content': text,
        }
      )
    decoded = decode(raw)
    return Leg.from_decoded(
      {'request': {'messages': items, 'tools': []}, 'response': raw},
      {},
      'chat',
      decoded,
      counts(decoded.usage),
    )

  wire.exchange = exchange
  monkeypatch.setattr(llm_retry, 'asyncio_sleep', pause)
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  monkeypatch.setattr(
    native_loop, 'create', lambda spec, inbox, **kwargs: native_loop.Loop(wire, inbox, **kwargs)
  )
  runner = Runner(create_bro('bro', llm_spec=EchoSpec()))
  if refusals == 3:
    assert await runner.run('original', surface='test') == 'reply'
  else:
    with pytest.raises(LLMStopped, match='insufficient_system_resource'):
      await runner.run('original', surface='test')
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  calls = [step for step in recorded.steps if step.kind == 'llm_call']
  assert [step.extras.get('stopped') for step in calls] == (
    ['capacity'] * 3 + [None] if refusals == 3 else ['capacity'] * 7
  )
  assert {step.extras['call_index'] for step in calls} == {1}
  assert {step.extras['turn_index'] for step in calls} == {0}
  assert all(step.body['request'] == calls[0].body['request'] for step in calls)
  assert all(request == requests[0] for request in requests)
  assert sum(waits) == expected_wait
  assert recorded.header.native['usage']['gpt-6.1-sol']['completion_tokens'] == len(calls) * 2
  header = store.get_trail(recorded.header.id)
  assert header['end']['reason'] == ('ok' if refusals == 3 else 'error')


@pytest.mark.asyncio
async def test_interrupt_received_calls_records_answers_and_continues_the_same_chain(
  server, replies, monkeypatch, tmp_path
):
  entered = asyncio.Event()

  async def ping() -> str:
    entered.set()
    await asyncio.Future()
    raise AssertionError('unreachable')

  original_create = native_loop.create

  def create(*args, **kwargs):
    loop = original_create(*args, **kwargs)
    loop.tools = ToolRegistry(
      [InProcessMCPServer('svc', [FunctionTool(ping, description='blocks')])]
    )
    return loop

  monkeypatch.setattr(native_loop, 'create', create)
  replies.append(response(call('{}'), id='with-calls'))
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(create_bro('bro', llm_spec=ResponsesSpec(model='gpt-6.1-sol')))
  with runner:
    turn = asyncio.create_task(runner.send('original', surface='test'))
    await entered.wait()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
      await turn
    assert await runner.send('continue', surface='test') == 'reply'
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  result = next(step for step in recorded.steps if step.kind == 'tool_result')
  assert result.body == native_loop.INTERRUPTED_TOOL_OUTPUT
  assert result.extras['is_error'] is True
  assert server[-1]['previous_response_id'] == 'with-calls'
  assert server[-1]['input'] == [
    {
      'type': 'function_call_output',
      'call_id': 'call_1',
      'output': native_loop.INTERRUPTED_TOOL_OUTPUT,
    },
    {'role': 'user', 'content': 'continue'},
  ]


@pytest.mark.asyncio
async def test_silent_turn_end_waits_on_the_background_job_and_wakes_on_its_exit(
  server, replies, monkeypatch, tmp_path
):
  release = tmp_path / 'release'
  replies.extend(
    [
      response(call(job_arguments(release), name=JOB_TOOL), id='started'),
      response({**message(''), 'phase': 'final_answer'}, id='silent'),
      response(message('done'), id='woken'),
    ]
  )
  store = LocalStore(tmp_path / 'trails')
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(ShellBro.create(ResponsesSpec(model='gpt-6.1-sol')))
  async with asyncio.timeout(60):
    assert await runner.run('go', observer=ReleaseWhenIdle(release), surface='test') == 'done'
  assert server[2]['previous_response_id'] == 'silent'
  [notification] = server[2]['input']
  assert notification['role'] == 'user'
  assert 'job-1' in notification['content'] and 'exited (code 0)' in notification['content']
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  assert not any('stopped' in step.extras for step in recorded.steps if step.kind == 'llm_call')
  assert store.get_trail(recorded.header.id)['end']['reason'] == 'ok'
