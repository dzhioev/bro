import dataclasses
import json
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bro.base import credentials
from bro.fork import fork, latest_fork_point
from bro.launch.resume import resume
from bro.llm.llm import LLMStopped
from bro.llm.llms.echo import LLMSpec as EchoSpec
from bro.llm.llms.openai import LLMSpec as ResponsesSpec
from bro.native import runner as native_runner
from bro.native.runner import Runner
from bro.native.wire_fixtures_test_helper import message, response
from bro.registry import create_bro
from bro.trails.local import LocalStore
from bro.trails.record.bro import Recorder
from bro.trails.store import fetch_recorded_trail


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
  'recipe', [EchoSpec(), ResponsesSpec(model='fixture-model')], ids=['echo', 'responses']
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
  runner = Runner(create_bro('bro', llm_spec=ResponsesSpec(model='fixture-model')))
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
    llm_spec=ResponsesSpec(model='fixture-model'),
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
