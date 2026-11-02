import asyncio
import json
import threading
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import openai
import pytest
from pydantic import BaseModel

import bro.native.harness as native_harness
import bro.run as bro_run
from bro.base import credentials
from bro.bro import BaseBro
from bro.deepseek.declaration import Recipe
from bro.fork import fork, latest_fork_point
from bro.launch.resume import resume
from bro.llm.llm import Recipe as BaseRecipe
from bro.llm.mcp import FunctionTool, InProcessMCPServer, ToolRegistry
from bro.llm.mu import mu
from bro.llm.observer import Observer, ReasoningEvent, ToolCallEvent
from bro.llm.wire import Text
from bro.native import loop as native_loop, runner as native_runner
from bro.native.runner import Runner
from bro.native.silent_turn_test_helper import JOB_TOOL, ReleaseWhenIdle, ShellBro, job_arguments
from bro.registry import create_bro
from bro.trails.local import LocalStore
from bro.trails.record.bro import Recorder
from bro.trails.store import fetch_recorded_trail
from bro.wire_chat.fixtures_test_helper import call, response
from bro.workspace.settings import HomeSettings
from ride import cli
from ride.authority_launch import credentials_for, hydrate, request
from ride.authority_reading import Configuration, Installation, Reading
from ride.authority_scope import checked_recipe
from ride.bro_worker import BroType
from ride.do_ride import SessionRun


@pytest.fixture
def endpoint(monkeypatch):
  requests = []
  replies = []
  authorizations = []

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      assert self.path in ('/chat/completions', '/responses')
      authorizations.append(self.headers['Authorization'])
      requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
      if self.path == '/responses':
        from bro.native.wire_fixtures_test_helper import message, response as responses_response

        fallback = responses_response(message('reply'))
      else:
        fallback = response()
      status, raw = replies.pop(0) if replies else (200, fallback)
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
    from bro.openai.declaration import Recipe as OpenAISpec

    for spec in (Recipe, OpenAISpec):
      monkeypatch.setattr(spec, 'ENDPOINT', f'http://127.0.0.1:{server.server_port}')
    yield requests, replies, authorizations


@pytest.fixture
def server(endpoint, monkeypatch):
  requests, replies, _ = endpoint
  monkeypatch.setattr(credentials, 'get_json', lambda kind: {'api_key': 'fixture-key'})
  return requests, replies


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
  runner = Runner(create_bro('bro'), Recipe(model='deepseek-v4-pro', reasoning_effort='high'))
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
  assert (
    recorded.header.native['usage'][Recipe(model='deepseek-v4-pro').model]['completion_tokens'] == 6
  )

  resumed = resume(
    store,
    'bro',
    runner.trail_id,
    llm_spec=Recipe.from_dict(recorded.header.native['llm']),
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
  runner = Runner(create_bro('bro'), Recipe(model='deepseek-v4-pro'))
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
  runner = Runner(create_bro('bro'), Recipe(model='deepseek-v4-pro'))
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
    llm_spec=Recipe.from_dict(recorded.header.native['llm']),
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
  )
  reading = Reading(
    Configuration.parse({}), {}, installation, settings=HomeSettings(summon_harness='bro')
  )
  monkeypatch.setattr(Reading, 'take', lambda repository: reading)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
  if present:
    (tmp_path / 'creds').mkdir()
    (tmp_path / 'creds' / 'deepseek.cred').write_text('{"api_key":"fixture-key"}')
  assert (
    cli.main(
      [
        'ride',
        'scope',
        '--bro',
        'child',
        '--harness',
        'bro',
        '--llm',
        'deepseek:deepseek-v4-pro,reasoning_effort=high',
      ]
    )
    == 0
  )
  output = capsys.readouterr().out
  assert '"provider": "deepseek"' in output
  assert f'deepseek+: {"PRESENT" if present else "MISSING"}' in output
  requested, _ = request(
    BroType,
    {'target': 'child', 'llm': 'deepseek:deepseek-v4-pro,reasoning_effort=high'},
    reading,
    reading.operator(),
    forward=False,
  )
  descriptor = reading.rules.normalize(requested, reading)
  assert descriptor.values['provider'] == 'deepseek'
  assert descriptor.values['model'] == Recipe(model='deepseek-v4-pro').model
  assert 'effort' not in descriptor.values
  assert descriptor.values['cred.deepseek'] == ''
  assert checked_recipe(
    descriptor, reading, ('deepseek:deepseek-v4-pro,reasoning_effort=high', 'summon llm')
  ) == Recipe(model='deepseek-v4-pro', reasoning_effort='high')


DEEPSEEK_KEY = 'host-deepseek-key'
OPENAI_KEY = 'host-openai-key'
MU_BACKINGS = {
  'openai': (
    '',
    {'llm': 'openai:gpt-5.6-terra,reasoning_effort=low', 'credential': {'$cred': 'openai+'}},
  ),
  'deepseek': (
    'deepseek',
    {'llm': 'deepseek:deepseek-flash,reasoning_effort=low', 'credential': {'$cred': 'deepseek+'}},
  ),
}


class Answer(BaseModel):
  answer: str


@dataclass(frozen=True)
class Stage:
  recipe: BaseRecipe
  files: dict[str, bytes]
  session: credentials.Store
  argv: list[str]


def summoned_stage(
  tmp_path, monkeypatch, backing: str, llm: str = 'deepseek:deepseek-v4-pro,reasoning_effort=high'
) -> Stage:
  instance, mu_material = MU_BACKINGS[backing]
  host = tmp_path / 'host'
  (host / 'creds').mkdir(parents=True)
  for name, material in {
    'deepseek': {'api_key': DEEPSEEK_KEY},
    'openai': {'api_key': OPENAI_KEY},
    'github': {'token': 'host-github-token'},
    'brog': {'backend': 'fixture'},
    credentials.storage_name('mu', instance): mu_material,
  }.items():
    (host / 'creds' / f'{name}.cred').write_text(json.dumps(material))
  monkeypatch.setattr(credentials, 'STORE_DIR', str(host))
  reading = Reading(
    Configuration.parse({}), {}, Installation.take(), settings=HomeSettings(summon_harness='bro')
  )
  fields = {
    'target': 'bro-dev',
    'llm': llm,
    **({'pass': [f'mu+{instance}']} if instance != '' else {}),
  }
  requested, _ = request(BroType, fields, reading, reading.operator(), forward=False)
  mission = reading.rules.start(requested, reading.operator(), reading)
  from ride.bro_worker import SummonLaunchSpec, _child_session_spec

  launch = SummonLaunchSpec(mission, 'go', 'parent', tmp_path, None, llm=llm)
  session_spec = _child_session_spec(launch, 'stage', None)
  recipe = session_spec.llm_spec
  files, _ = hydrate(credentials_for(mission))
  directory = tmp_path / 'session'
  for name, content in files.items():
    (directory / name).parent.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
  run = SessionRun(
    name='stage',
    repo=None,
    harness='bro',
    hold='unattended',
    resolved_llm=session_spec.resolved_llm,
    solo=True,
    resume=False,
    bro='bro-dev',
    prompt='go',
    activity_file=tmp_path / 'activity',
  )
  spawned: list[list[str]] = []
  monkeypatch.setattr(native_harness.spawn, 'console_script', lambda name: '/venv/bin/bro')
  monkeypatch.setattr('ride.do_ride.run_agent', lambda argv: spawned.append(argv) or 0)
  assert native_harness.BRO.run_session(run) == 0
  session = credentials.Store(credentials.default_registry(), directory, {})
  return Stage(recipe, files, session, spawned[0])


@pytest.mark.credential_gate
@pytest.mark.parametrize('backing', MU_BACKINGS)
def test_a_stage_summoned_onto_deepseek_starts_without_the_openai_key(
  endpoint, tmp_path, monkeypatch, backing
):
  requests, _, authorizations = endpoint
  stage = summoned_stage(tmp_path, monkeypatch, backing)
  assert stage.session.available('deepseek') and not stage.session.available('openai')
  monkeypatch.setattr(
    native_runner, '_default_tracker_factory', lambda: Recorder(LocalStore(tmp_path / 'trails'))
  )
  monkeypatch.setattr('bro.launch.broxy.session_broxy', nullcontext)
  with credentials.as_default_store(stage.session):
    assert bro_run.main(stage.argv) is None
  assert requests[0]['model'] == stage.recipe.model
  assert set(authorizations) == {f'Bearer {DEEPSEEK_KEY}'}


def test_a_deepseek_backed_mu_serves_a_stage_that_holds_no_openai_material(
  endpoint, tmp_path, monkeypatch
):
  requests, replies, authorizations = endpoint
  replies.append((200, response('{"answer": "42"}')))
  stage = summoned_stage(tmp_path, monkeypatch, 'deepseek')
  assert all(OPENAI_KEY.encode() not in content for content in stage.files.values())
  with credentials.as_default_store(stage.session):
    assert mu('which answer?', Answer, Text('the record'), role='dispatch').answer == '42'
  sent = requests[0]
  assert sent['response_format'] == {'type': 'json_object'}
  assert sent['model'] == 'deepseek-flash'
  assert sent['reasoning_effort'] == 'low'
  assert authorizations == [f'Bearer {DEEPSEEK_KEY}']


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
  runner = Runner(create_bro('bro'), Recipe(model='deepseek-v4-pro'))
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
    assert (
      recorded.header.native['usage'][Recipe(model='deepseek-v4-pro').model]['completion_tokens']
      == 3
    )
    assert latest_fork_point(recorded) < rejected[0].step_id
    resumed = resume(
      store, 'bro', runner.trail_id, llm_spec=Recipe(model='deepseek-v4-pro'), hold='unattended'
    )
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
  assert (
    continued.header.native['usage'][Recipe(model='deepseek-v4-pro').model]['completion_tokens']
    == 6
  )


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
  with Runner(create_bro('bro-dev'), Recipe(model='deepseek-v4-pro')) as runner:
    standalone = tools_to_chat_format(
      await ToolRegistry(
        runner.bro.assemble(harness=BRO, hold='unattended', live_run=runner)
      ).resolve()
    )
  assert standalone is not None
  managed = await managed_roster(monkeypatch, Recipe(model='deepseek-v4-pro'))
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
  runner = Runner(ShellBro(), Recipe(model='deepseek-v4-pro'))
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


@pytest.mark.asyncio
@pytest.mark.parametrize('first_reasoning', ['first thought', ''])
@pytest.mark.parametrize('missing_status', [200, 400])
@pytest.mark.parametrize('transition_status', [200, 400])
async def test_live_probe_reaches_every_leg_with_empty_reasoning(
  server, monkeypatch, tmp_path, first_reasoning, missing_status, transition_status
):
  from copy import deepcopy

  from bro.deepseek import deepseek_llm_test

  requests, replies = server
  empty = response('', reasoning='')
  empty['usage']['completion_tokens_details']['reasoning_tokens'] = 0
  first = response(
    '', reasoning=first_reasoning, calls=[call(name='first_probe')], stop='tool_calls'
  )
  if not first_reasoning:
    first['usage']['completion_tokens_details']['reasoning_tokens'] = 0
  second = response(
    '', reasoning='', calls=[call(name='second_probe')], stop='tool_calls', usage=empty['usage']
  )
  error = {'error': {'message': 'missing reasoning_content', 'type': 'invalid_request_error'}}
  replies.extend(
    [
      (200, first),
      (200, second),
      (200, empty),
      (200, empty),
      (200, empty),
      (missing_status, empty if missing_status == 200 else error),
      (200, empty),
      (transition_status, empty if transition_status == 200 else error),
      (200, response('', reasoning='', stop='length')),
    ]
  )
  monkeypatch.setattr(credentials, 'available', lambda kind: True)
  request = Mock()
  await deepseek_llm_test.test_deepseek_protocol_shapes_and_native_tool_roster(
    tmp_path, request, monkeypatch
  )
  assert not replies
  assert len(requests) == 9
  assert requests[2]['messages'][-3]['reasoning_content'] == ''
  assert requests[3]['messages'][-2]['content'] == ''
  assert requests[3]['messages'][-2]['reasoning_content'] == ''
  without_reasoning = deepcopy(requests[1])
  for message in without_reasoning['messages']:
    message.pop('reasoning_content', None)
  assert requests[5] == without_reasoning
  assert requests[6]['thinking'] == {'type': 'disabled'}
  assert requests[7]['thinking'] == {'type': 'enabled'}
  assert requests[7]['messages'][1]['reasoning_content'] == ''
  assert requests[8]['max_tokens'] == 1
  captures = json.loads((tmp_path / 'deepseek-responses.json').read_text())
  assert list(captures) == [
    'first-leg',
    'second-leg',
    'third-leg',
    'after-empty-turn',
    'native-roster',
    'native-roster-tool-count',
    'missing-reasoning',
    'thinking-off',
    'thinking-off-to-on',
    'forced-length',
  ]
  assert captures['native-roster-tool-count'] == len(requests[4]['tools'])
  for name, status in [
    ('missing-reasoning', missing_status),
    ('thinking-off-to-on', transition_status),
  ]:
    if status == 400:
      assert captures[name]['status'] == status
      assert json.loads(captures[name]['body']) == error
    else:
      assert captures[name]['choices'][0]['finish_reason'] == 'stop'


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'message',
  [
    {'content': None, 'refusal': 'not allowed'},
    {'content': '', 'refusal': 'not allowed'},
    {'content': None, 'refusal': ''},
    {'content': '', 'refusal': ''},
    {'content': '', 'tool_calls': [call(call_id='')]},
  ],
)
async def test_live_probe_rejects_an_unusable_third_reply(server, monkeypatch, tmp_path, message):
  from bro.deepseek import deepseek_llm_test

  requests, replies = server
  third = response('', reasoning='')
  third['choices'][0]['message'].update(message)
  replies.extend(
    [
      (200, response('', reasoning='', calls=[call(name='first_probe')], stop='tool_calls')),
      (200, response('', reasoning='', calls=[call(name='second_probe')], stop='tool_calls')),
      (200, third),
    ]
  )
  monkeypatch.setattr(credentials, 'available', lambda kind: True)
  with pytest.raises(AssertionError):
    await deepseek_llm_test.test_deepseek_protocol_shapes_and_native_tool_roster(
      tmp_path, Mock(), monkeypatch
    )
  assert not replies
  assert len(requests) == 3
  captures = json.loads((tmp_path / 'deepseek-responses.json').read_text())
  assert captures['third-leg']['choices'][0]['message']['content'] == message['content']


@pytest.mark.credential_gate
def test_unlisted_openai_summon_carries_passthrough_through_worker_and_session(
  endpoint, tmp_path, monkeypatch
):
  requests, _, authorizations = endpoint
  stage = summoned_stage(tmp_path, monkeypatch, 'deepseek', 'openai:gpt-7+passthrough')
  assert stage.recipe.passthrough
  monkeypatch.setattr(
    native_runner, '_default_tracker_factory', lambda: Recorder(LocalStore(tmp_path / 'trails'))
  )
  monkeypatch.setattr('bro.launch.broxy.session_broxy', nullcontext)
  with credentials.as_default_store(stage.session):
    assert bro_run.main(stage.argv) is None
  assert requests[0]['model'] == 'gpt-7'
  assert authorizations == [f'Bearer {OPENAI_KEY}']
