import asyncio
import json
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest

from bro.base import credentials
from bro.bro import BaseBro
from bro.deepseek.declaration import LLMSpec
from bro.fork import fork, latest_fork_point
from bro.launch.resume import resume
from bro.llm.mcp import FunctionTool, InProcessMCPServer, ToolRegistry
from bro.llm.observer import Observer, ReasoningEvent, ToolCallEvent
from bro.native import loop as native_loop, runner as native_runner
from bro.native.runner import Runner
from bro.native.silent_turn_test_helper import JOB_TOOL, ReleaseWhenIdle, ShellBro, job_arguments
from bro.registry import create_bro
from bro.trails.local import LocalStore
from bro.trails.record.bro import Recorder
from bro.trails.store import fetch_recorded_trail
from bro.wire_chat.fixtures_test_helper import call, response
from ride import cli
from ride.authority_launch import request
from ride.authority_reading import Configuration, Installation, Reading
from ride.authority_scope import checked_recipe
from ride.bro_worker import BroType


@pytest.fixture
def server(monkeypatch):
  requests = []
  replies = []

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      assert self.path == '/chat/completions'
      requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
      status, raw = replies.pop(0) if replies else (200, response())
      payload = json.dumps(raw).encode()
      self.send_response(status)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, format, *args):
      pass

  with ExitStack() as resources:
    server = resources.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Handler))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    resources.callback(thread.join)
    resources.callback(server.shutdown)
    monkeypatch.setattr(LLMSpec, 'ENDPOINT', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setattr(credentials, 'get_json', lambda kind: {'api_key': 'fixture-key'})
    yield requests, replies


@pytest.mark.asyncio
async def test_run_tool_loop_projection_resume_and_fork(server, monkeypatch, tmp_path):
  requests, replies = server
  replies.extend(
    [
      (200, response(None, reasoning='first thought', calls=[call('')], stop='tool_calls')),
      (200, response('answer', reasoning='second thought')),
    ]
  )
  called = []

  def ping() -> dict[str, int]:
    called.append(True)
    return {'value': 42}

  create = native_loop.create

  def with_tools(*args, **kwargs):
    loop = create(*args, **kwargs)
    loop.tools = ToolRegistry([InProcessMCPServer('svc', [FunctionTool(ping, description='Ping')])])
    return loop

  monkeypatch.setattr(native_loop, 'create', with_tools)
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(create_bro('bro', llm_spec=LLMSpec(reasoning_effort='high')))
  assert await runner.run('start', surface='test') == 'answer'
  assert called == [True]
  assert requests[1]['messages'][2] == {
    'role': 'assistant',
    'content': None,
    'reasoning_content': 'first thought',
    'tool_calls': [call('')],
  }
  assert requests[1]['messages'][3] == {
    'role': 'tool',
    'tool_call_id': 'call_1',
    'content': '{"value":42}',
  }
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  calls = [step for step in recorded.steps if step.kind == 'llm_call']
  assert all(step.extras['wire'] == 'chat' for step in calls)
  assert calls[1].body['request']['messages'] == [requests[1]['messages'][3]]
  projected = list(store.iter_messages(runner.trail_id))
  assert [
    (event['type'], event['content'])
    for event in projected
    if event['type'] in {'reasoning', 'assistant'}
  ] == [('reasoning', 'first thought'), ('reasoning', 'second thought'), ('assistant', 'answer')]
  tool_call = next(event for event in projected if event['type'] == 'tool_call')
  result = next(step for step in recorded.steps if step.kind == 'tool_result')
  assert result.extras['call_id'] == tool_call['call_id']
  assert tool_call['source'] == {'step_id': calls[0].step_id, 'index': 2}
  assert recorded.header.native['usage'][LLMSpec().model]['completion_tokens'] == 6

  resumed = resume(
    store,
    'bro',
    runner.trail_id,
    llm_spec=LLMSpec.from_dict(recorded.header.native['llm']),
    hold='unattended',
  )
  with resumed.runner:
    assert await resumed.runner.send('continue', surface='test') == 'reply'
  expected = requests[1]['messages'] + [
    {'role': 'assistant', 'content': 'answer', 'reasoning_content': 'second thought'},
    {'role': 'user', 'content': 'continue'},
  ]
  assert requests[-1]['messages'] == expected
  restored = fork(recorded, latest_fork_point(recorded), hold='unattended', surface='test')
  with restored:
    assert await restored.send('continue', surface='test') == 'reply'
  assert requests[-1]['messages'] == expected
  assert restored.trail_id is not None
  child = fetch_recorded_trail(store, restored.trail_id)
  assert child.header.forked_from is not None
  assert child.header.forked_from.trail_id == runner.trail_id
  assert not any('previous_response_id' in request for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [402, 429, 500])
async def test_http_errors_follow_provider_retry_policy(server, monkeypatch, status):
  requests, replies = server
  replies.append(
    (status, {'error': {'message': 'fixture error', 'type': 'api_error', 'code': str(status)}})
  )
  waits = []

  async def wait(seconds):
    waits.append(seconds)

  monkeypatch.setattr('bro.base.retry.asyncio_sleep', wait)
  runner = Runner(create_bro('bro', llm_spec=LLMSpec()))
  if status == 402:
    with pytest.raises(openai.APIStatusError) as caught:
      await runner.run('go', surface='test')
    assert caught.value.status_code == 402
    assert len(requests) == 1 and not waits
  else:
    assert await runner.run('go', surface='test') == 'reply'
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert waits == [1]


@pytest.mark.asyncio
async def test_http_capacity_stop_is_recorded_and_retried_without_entering_history(
  server, monkeypatch, tmp_path
):
  requests, replies = server
  replies.append(
    (200, response('discard', reasoning='discard', stop='insufficient_system_resource'))
  )
  waits = []

  async def wait(seconds):
    waits.append(seconds)

  monkeypatch.setattr('bro.llm.retry.asyncio_sleep', wait)
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(create_bro('bro', llm_spec=LLMSpec()))
  assert await runner.run('go', surface='test') == 'reply'
  assert waits == [10]
  assert requests[0] == requests[1]
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  calls = [step for step in recorded.steps if step.kind == 'llm_call']
  assert [step.extras.get('stopped') for step in calls] == ['capacity', None]
  assert [step.extras['call_index'] for step in calls] == [1, 1]
  resumed = resume(
    store,
    'bro',
    runner.trail_id,
    llm_spec=LLMSpec.from_dict(recorded.header.native['llm']),
    hold='unattended',
  )
  with resumed.runner:
    assert await resumed.runner.send('continue', surface='test') == 'reply'
  assert not any(item.get('reasoning_content') == 'discard' for item in requests[-1]['messages'])


@pytest.mark.parametrize('present', [False, True])
def test_scope_and_summon_lower_the_selected_recipe_without_material_reads(
  monkeypatch, tmp_path, capsys, present
):
  class Child(BaseBro):
    name = 'child'

  installation = Installation.take()
  from dataclasses import replace

  installation = replace(
    installation,
    bros={**installation.bros, 'child': Child()},
    harnesses={
      name: replace(harness, recipes={**harness.recipes, 'child': None})
      for name, harness in installation.harnesses.items()
    },
  )
  reading = Reading(Configuration.parse({}), {}, installation, None, {'summon-harness': 'bro'})
  monkeypatch.setattr(Reading, 'take', lambda repository: reading)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
  if present:
    (tmp_path / 'creds').mkdir()
    (tmp_path / 'creds' / 'deepseek.cred').write_text('{"api_key":"fixture-key"}')
  assert (
    cli.main(['ride', 'scope', '--bro', 'child', '--harness', 'bro', '--llm', 'deepseek:pro:high'])
    == 0
  )
  output = capsys.readouterr().out
  assert '"provider": "deepseek"' in output
  assert f'deepseek+: {"PRESENT" if present else "MISSING"}' in output
  requested, _ = request(
    BroType,
    {'target': 'child', 'llm': 'deepseek:pro:high'},
    reading,
    reading.operator(),
    forward=False,
  )
  descriptor = reading.rules.normalize(requested, reading)
  assert descriptor.values['provider'] == 'deepseek'
  assert descriptor.values['model'] == LLMSpec().model
  assert descriptor.values['effort'] == 'high'
  assert descriptor.values['cred.deepseek'] == ''
  assert checked_recipe(requested, descriptor, reading) == LLMSpec(reasoning_effort='high')


@pytest.mark.asyncio
async def test_rejected_completion_preserves_live_inputs_and_matches_resume(
  server, monkeypatch, tmp_path
):
  requests, replies = server
  replies.append(
    (200, response(None, reasoning='discard', calls=[call(call_id='')], stop='tool_calls'))
  )
  store = LocalStore(tmp_path)
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(create_bro('bro', llm_spec=LLMSpec()))
  runner.inbox.post('notice')
  events = []

  class Observed(Observer):
    def on_event(self, event):
      events.append(event)

  with runner:
    with pytest.raises(RuntimeError):
      await runner.send('original', observer=Observed(), surface='test', hold='unattended')
    assert not any(isinstance(event, (ReasoningEvent, ToolCallEvent)) for event in events)
    assert runner.trail_id is not None
    recorded = fetch_recorded_trail(store, runner.trail_id)
    rejected = [step for step in recorded.steps if step.kind == 'llm_call']
    assert len(rejected) == 1
    assert rejected[0].extras['stopped'] == 'failed'
    assert rejected[0].body['response']['choices'][0]['message']['reasoning_content'] == 'discard'
    assert recorded.header.native['usage'][LLMSpec().model]['completion_tokens'] == 3
    assert latest_fork_point(recorded) < rejected[0].step_id
    resumed = resume(store, 'bro', runner.trail_id, llm_spec=LLMSpec(), hold='unattended')
    with resumed.runner:
      assert await resumed.runner.send('continue', surface='test') == 'reply'
    assert await runner.send('continue', surface='test') == 'reply'
  expected = requests[0]['messages'] + [{'role': 'user', 'content': 'continue'}]
  assert requests[1]['messages'] == requests[2]['messages'] == expected
  assert not any(item.get('role') == 'assistant' for item in requests[1]['messages'])
  assert [item['content'] for item in requests[0]['messages'] if item['role'] == 'user'] == [
    'notice',
    'original',
  ]
  assert runner.trail_id is not None
  continued = fetch_recorded_trail(store, runner.trail_id)
  assert continued.header.native['usage'][LLMSpec().model]['completion_tokens'] == 6


@pytest.mark.asyncio
async def test_live_probe_roster_includes_the_managed_summoned_service_schemas(monkeypatch):
  from bro.broker.client import Client
  from bro.deepseek.probe_test_helper import managed_roster
  from bro.native.harness import BRO
  from bro.wire_chat.client import tools_to_chat_format

  monkeypatch.setattr(credentials, 'available', lambda kind: True)
  monkeypatch.setattr(credentials, 'get_json', lambda kind: pytest.fail('credential material read'))
  monkeypatch.setattr(Client, 'from_env', lambda: pytest.fail('broker connection'))
  standalone = None
  with Runner(create_bro('bro-dev', llm_spec=LLMSpec())) as runner:
    standalone = tools_to_chat_format(
      await ToolRegistry(
        runner.bro.assemble(harness=BRO, hold='unattended', live_run=runner)
      ).resolve()
    )
  assert standalone is not None
  managed = await managed_roster(monkeypatch, LLMSpec())
  standalone_by_name = {tool['function']['name']: tool for tool in standalone}
  managed_by_name = {tool['function']['name']: tool for tool in managed}
  assert {name: managed_by_name[name] for name in standalone_by_name} == standalone_by_name
  assert managed_by_name.keys() - standalone_by_name.keys() == {
    'bro__answer',
    'bro__summon',
    'bro__quest_check',
    'bro__quest_history',
    'bro__quest_say',
    'bro__quest_ask',
    'bro__quest_share',
    'bro__quest_list',
    'bro__quest_cancel',
    'bro__quest_watch',
  }
  assert all(tool['function']['parameters']['type'] == 'object' for tool in managed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'silent,replayed',
  [
    (response(None), {'role': 'assistant', 'content': ''}),
    (
      response('', reasoning='waiting'),
      {'role': 'assistant', 'content': '', 'reasoning_content': 'waiting'},
    ),
  ],
  ids=['empty', 'reasoning-only'],
)
async def test_silent_turn_end_waits_on_the_background_job_and_wakes_on_its_exit(
  server, monkeypatch, tmp_path, silent, replayed
):
  requests, replies = server
  release = tmp_path / 'release'
  replies.extend(
    [
      (200, response(None, calls=[call(job_arguments(release), name=JOB_TOOL)], stop='tool_calls')),
      (200, silent),
      (200, response('done')),
    ]
  )
  store = LocalStore(tmp_path / 'trails')
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  runner = Runner(ShellBro.create(LLMSpec()))
  async with asyncio.timeout(60):
    assert await runner.run('go', observer=ReleaseWhenIdle(release), surface='test') == 'done'
  *history, notification = requests[2]['messages']
  assert history == requests[1]['messages'] + [replayed]
  assert notification['role'] == 'user'
  assert 'job-1' in notification['content'] and 'exited (code 0)' in notification['content']
  assert runner.trail_id is not None
  recorded = fetch_recorded_trail(store, runner.trail_id)
  assert not any('stopped' in step.extras for step in recorded.steps if step.kind == 'llm_call')
  assert store.get_trail(recorded.header.id)['end']['reason'] == 'ok'
