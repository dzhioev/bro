import asyncio
import os
import threading
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import MagicMock, call

import httpx2
import pytest
from openai import APIStatusError, APITimeoutError
from openai.types.responses import Response

import bro.llm.usage as usage
import bro.native.loop as loop_module
from bro.inbox import Inbox
from bro.jobs import Job, Registry
from bro.llm.llm import LLMStopped
from bro.llm.mcp import (
  FunctionTool,
  InProcessMCPServer,
  Tool,
  ToolControlSignal,
  ToolRegistry,
  encode_tool_result,
  wire_name,
)
from bro.llm.observer import (
  InterimAssistantTextEvent,
  NotificationEvent,
  Observer,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
)
from bro.llm.tracker import Tracker
from bro.llm.wire import File, SystemPrompt, Text, ToolOutput, UserMessage
from bro.native.loop_test_helper import ResponsesLoop, make_loop
from bro.trails.local import LocalStore
from bro.trails.record.bro import Recorder
from bro.wire_responses.client import parse_response

# the registry advertises namespaced wire names, so a tool whose local name is
# `ping` in this namespace surfaces to the LLM as `svc__ping`. the emit helpers
# below wrap the local name the same way, modeling what the model calls back.
_TEST_NAMESPACE = 'svc'
_REQUEST = httpx2.Request('POST', 'https://api.example.test/v1/responses')


class _StaticTool(Tool):
  def __init__(self, name: str, raise_with: Optional[Exception] = None):
    self._name = name
    self._raise_with = raise_with

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    return 'test tool'

  @property
  def parameters(self) -> dict:
    return {'type': 'object', 'properties': {}}

  async def call(self, arguments: dict) -> str:
    if self._raise_with is not None:
      raise self._raise_with
    return 'ok'


def _function_call_response(name: str):
  from bro.llm.wire import Leg
  from bro.wire_responses.decode import decode

  raw = {
    'model': 'test',
    'status': 'completed',
    'output': [
      {
        'type': 'function_call',
        'name': wire_name(_TEST_NAMESPACE, name),
        'arguments': '{}',
        'call_id': 'call_1',
      }
    ],
  }
  return Leg.from_decoded({'request': {}, 'response': raw}, {}, 'responses', decode(raw), None)


def _make_responses_loop(tools: list[Tool]) -> ResponsesLoop:
  gpt = make_loop(api_key='dummy', inbox=Inbox())
  gpt.tools = ToolRegistry([InProcessMCPServer(_TEST_NAMESPACE, tools)])
  return gpt


class _RecordingTracker(Tracker):
  """captures every tracker call so tests can assert kind/body/extras."""

  def __init__(self):
    self.headers: list[dict] = []
    self.steps: list[tuple[str, Any, dict]] = []
    self.ended: list[str] = []

  def start_trail(
    self,
    bro,
    llm_spec,
    system_prompt,
    forked_from,
    interactive,
    surface,
    hold='unattended',
    summoned_by=None,
  ) -> str:
    self.headers.append(
      {
        'bro': bro,
        'llm_spec': llm_spec,
        'system_prompt': system_prompt,
        'forked_from': forked_from,
        'interactive': interactive,
        'surface': surface,
      }
    )
    return 'tid'

  def step(self, kind, body, **extras) -> int:
    self.steps.append((kind, body, extras))
    return len(self.steps) - 1

  def end_trail(self, reason, detail=None) -> None:
    self.ended.append(reason)


def _fake_usage(
  *, input_tokens=10, output_tokens=20, reasoning_tokens=5, cached_tokens=0, cache_write_tokens=0
):
  return SimpleNamespace(
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    input_tokens_details=SimpleNamespace(
      cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens
    ),
  )


def _fake_response(
  *, output, response_id='resp_1', usage=None, dump_payload=None, model='gpt-5'
) -> Response:
  namespace = SimpleNamespace(
    id=response_id,
    output=output,
    usage=usage if usage is not None else _fake_usage(),
    model=model,
  )

  def plain(value):
    if isinstance(value, SimpleNamespace):
      return {key: plain(item) for key, item in vars(value).items()}
    if isinstance(value, list):
      return [plain(item) for item in value]
    return value

  payload = {
    'id': response_id,
    'status': 'completed',
    'model': model,
    'output': plain(output),
    'usage': plain(namespace.usage),
  }
  if dump_payload is not None:
    payload.update(dump_payload)
  namespace.model_dump = lambda mode='json': payload
  return cast(Response, namespace)


def _reasoning_item(*texts: str):
  return SimpleNamespace(
    type='reasoning',
    summary=[SimpleNamespace(type='summary_text', text=t) for t in texts],
  )


def _message_item(text: str, *, phase: Optional[str] = None):
  return SimpleNamespace(
    type='message',
    phase=phase,
    content=[SimpleNamespace(type='output_text', text=text)],
  )


def _function_call_item(name: str, *, call_id: str, arguments='{}'):
  return SimpleNamespace(
    type='function_call',
    name=wire_name(_TEST_NAMESPACE, name),
    arguments=arguments,
    call_id=call_id,
  )


def _make_responses_loop_with_tracker(
  tools: Optional[list[Tool]] = None,
  *,
  reasoning_effort=None,
  compact_threshold: Optional[int] = None,
  agent: Optional[str] = None,
  inbox: Optional[Inbox] = None,
) -> tuple[ResponsesLoop, _RecordingTracker, list[dict]]:
  gpt = make_loop(
    api_key='dummy',
    inbox=inbox if inbox is not None else Inbox(),
    reasoning_effort=reasoning_effort,
    compact_threshold=compact_threshold,
    agent=agent,
  )
  gpt.tools = ToolRegistry(
    [InProcessMCPServer(_TEST_NAMESPACE, tools)] if tools is not None else []
  )
  gpt._resolved_tools = []
  tracker = _RecordingTracker()
  gpt.tracker = tracker
  captured: list[dict] = []
  return gpt, tracker, captured


def _install_responses(gpt: ResponsesLoop, sequence: list, captured: list[dict]) -> None:
  iterator = iter(sequence)

  async def create(**kwargs):
    captured.append(kwargs)
    return next(iterator)

  # OpenAI client exposes `responses` as a cached_property — write through to
  # a duck-typed namespace instead by replacing `gpt.wire.client` wholesale. cast
  # keeps pyright quiet since the attribute is typed as the OpenAI class.
  gpt.wire.client = cast(Any, SimpleNamespace(responses=SimpleNamespace(create=create)))


def _install_failing_create(gpt: ResponsesLoop, captured: list[dict], error: Exception) -> None:
  """fail the first create with `error`, then answer 'ok'"""

  async def create(**kwargs):
    captured.append(kwargs)
    if len(captured) == 1:
      raise error
    return _fake_response(output=[_message_item('ok')])

  gpt.wire.client = cast(Any, SimpleNamespace(responses=SimpleNamespace(create=create)))


async def _no_wait(seconds: float) -> None:
  del seconds


@pytest.mark.asyncio
async def test_tool_exception_becomes_function_call_output():
  tool = _StaticTool('boom', raise_with=RuntimeError('upstream down'))
  gpt = _make_responses_loop([tool])

  results = await gpt._execute_tool_calls(
    _function_call_response('boom'), turn_index=0, call_index=1
  )

  assert len(results) == 1
  result = results[0]
  assert isinstance(result, ToolOutput)
  assert result.call_id == 'call_1'
  assert "'svc__boom' failed" in result.output
  assert 'RuntimeError' in result.output
  assert 'upstream down' in result.output


@pytest.mark.asyncio
async def test_tool_control_signal_propagates_past_loop():
  class _Abort(ToolControlSignal):
    pass

  tool = _StaticTool('halt', raise_with=_Abort('stop the run'))
  gpt = _make_responses_loop([tool])

  with pytest.raises(_Abort, match='stop the run'):
    await gpt._execute_tool_calls(_function_call_response('halt'), turn_index=0, call_index=1)


@pytest.mark.asyncio
async def test_successful_tool_call_returns_output_unchanged():
  gpt = _make_responses_loop([_StaticTool('ping')])

  results = await gpt._execute_tool_calls(
    _function_call_response('ping'), turn_index=0, call_index=1
  )

  assert len(results) == 1
  assert cast(ToolOutput, results[0]).output == 'ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Привет', '漢字'])
async def test_structured_tool_output_is_compact_unicode_but_tracker_keeps_object(text):
  def document() -> dict[str, list[str]]:
    return {'pages': [text, 'second']}

  gpt, tracker, _ = _make_responses_loop_with_tracker(
    [FunctionTool(document, description='read a document')]
  )
  results = await gpt._execute_tool_calls(
    _function_call_response('document'), turn_index=0, call_index=1
  )

  assert (
    encode_tool_result(cast(ToolOutput, results[0]).output) == '{"pages":["' + text + '","second"]}'
  )
  assert [(kind, body) for kind, body, _ in tracker.steps if kind == 'tool_result'] == [
    ('tool_result', {'pages': [text, 'second']})
  ]


class TestToolResultTrackerEmission:
  @pytest.mark.asyncio
  async def test_emits_tool_result_with_call_id_and_is_error_false(self):
    gpt, tracker, _ = _make_responses_loop_with_tracker([_StaticTool('ping')])
    await gpt._execute_tool_calls(_function_call_response('ping'), turn_index=2, call_index=4)

    results = [s for s in tracker.steps if s[0] == 'tool_result']
    assert len(results) == 1
    kind, body, extras = results[0]
    assert body == 'ok'
    assert extras == {
      'turn_index': 2,
      'call_index': 4,
      'tool_name': 'svc__ping',
      'call_id': 'call_1',
      'is_error': False,
    }

  @pytest.mark.asyncio
  async def test_emits_tool_result_with_is_error_true_on_exception(self):
    tool = _StaticTool('boom', raise_with=RuntimeError('upstream down'))
    gpt, tracker, _ = _make_responses_loop_with_tracker([tool])
    await gpt._execute_tool_calls(_function_call_response('boom'), turn_index=3, call_index=5)

    results = [s for s in tracker.steps if s[0] == 'tool_result']
    assert len(results) == 1
    _, body, extras = results[0]
    assert "'svc__boom' failed" in body
    assert extras['is_error'] is True
    assert extras['turn_index'] == 3
    assert extras['call_index'] == 5
    assert extras['call_id'] == 'call_1'


class _LoopBlockingStore(LocalStore):
  def __init__(self, root: Path):
    super().__init__(root)
    self.append_started = threading.Event()
    self.loop_responsive = threading.Event()
    self.loop_was_responsive: Optional[bool] = None

  def append_records(
    self,
    trail_id: str,
    offset: int,
    records: list[Any],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    if self.loop_was_responsive is None:
      self.append_started.set()
      self.loop_was_responsive = self.loop_responsive.wait(timeout=1.0)
    return super().append_records(trail_id, offset, records, tools=tools)


class TestSendTrackerEmission:
  @pytest.mark.asyncio
  async def test_slow_store_does_not_block_the_event_loop(self, tmp_path):
    store = _LoopBlockingStore(tmp_path)
    with closing(Recorder(store)) as tracker:
      tracker.start_trail(
        bro='dev',
        llm_spec={'type': 'openai', 'model': 'gpt-5'},
        system_prompt='test',
        forked_from=None,
        interactive=False,
        surface='test',
      )
      gpt, _, captured = _make_responses_loop_with_tracker()
      gpt.tracker = tracker
      _install_responses(gpt, [_fake_response(output=[_message_item('done')])], captured)

      turn = asyncio.create_task(gpt.send([UserMessage((Text('go'),))]))
      while not store.append_started.is_set():
        await asyncio.sleep(0)
      store.loop_responsive.set()
      assert await turn == 'done'

    assert store.loop_was_responsive is True

  @pytest.mark.asyncio
  async def test_user_input_emitted_at_turn_zero_skipping_system(self):
    gpt, tracker, captured = _make_responses_loop_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('hi back')])], captured)

    await gpt.send([SystemPrompt('be helpful'), UserMessage((Text('hello'),))])

    user_steps = [s for s in tracker.steps if s[0] == 'user_input']
    assert len(user_steps) == 1
    _, body, extras = user_steps[0]
    assert body == 'hello'
    assert extras == {'turn_index': 0}

  @pytest.mark.asyncio
  @pytest.mark.parametrize(
    'content,expected',
    [
      (
        (Text('caption this'), File(b'image', 'image/png'), Text('next line')),
        'caption this\nnext line',
      ),
      ((File(b'image', 'image/png'),), ''),
    ],
  )
  async def test_user_input_text_extracted_from_multimodal_content(self, content, expected):
    gpt, tracker, captured = _make_responses_loop_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ack')])], captured)

    await gpt.send([UserMessage(content)])

    user_steps = [s for s in tracker.steps if s[0] == 'user_input']
    assert user_steps[0][1] == expected

  @pytest.mark.asyncio
  async def test_request_timeout_forwarded_to_responses_create(self):
    gpt, _, captured = _make_responses_loop_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ok')])], captured)

    await gpt.send([UserMessage((Text('hi'),))], request_timeout=120.0)

    assert captured[0]['timeout'] == 120.0

  @pytest.mark.asyncio
  async def test_request_timeout_omitted_leaves_client_default(self):
    gpt, _, captured = _make_responses_loop_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ok')])], captured)

    await gpt.send([UserMessage((Text('hi'),))])

    assert 'timeout' not in captured[0]

  @pytest.mark.asyncio
  async def test_a_transient_failure_is_retried(self, monkeypatch):
    gpt, _, captured = _make_responses_loop_with_tracker()
    _install_failing_create(gpt, captured, APITimeoutError(request=_REQUEST))
    monkeypatch.setattr('bro.base.retry.asyncio_sleep', _no_wait)

    assert await gpt.send([UserMessage((Text('hi'),))]) == 'ok'
    assert len(captured) == 2

  @pytest.mark.asyncio
  async def test_a_client_error_is_not_retried(self, monkeypatch):
    gpt, _, captured = _make_responses_loop_with_tracker()
    error = APIStatusError('bad', response=httpx2.Response(400, request=_REQUEST), body=None)
    _install_failing_create(gpt, captured, error)
    monkeypatch.setattr('bro.base.retry.asyncio_sleep', _no_wait)

    with pytest.raises(APIStatusError):
      await gpt.send([UserMessage((Text('hi'),))])
    assert len(captured) == 1

  @pytest.mark.asyncio
  async def test_llm_call_records_request_response_and_indexes(self):
    gpt, tracker, captured = _make_responses_loop_with_tracker()
    response = _fake_response(
      output=[_message_item('reply')],
      response_id='resp_xyz',
      usage=_fake_usage(input_tokens=11, output_tokens=22, reasoning_tokens=33, cached_tokens=7),
      dump_payload={'extra': 'retained'},
    )
    _install_responses(gpt, [response], captured)

    await gpt.send([UserMessage((Text('hi'),))])

    llm_calls = [s for s in tracker.steps if s[0] == 'llm_call']
    assert len(llm_calls) == 1
    _, body, extras = llm_calls[0]
    assert body['response']['extra'] == 'retained'
    assert body['response']['output'][0]['content'][0]['text'] == 'reply'
    assert body['request']['model'] == gpt.wire.recipe.model
    # request kwargs round-tripped, including the input list passed to the API.
    assert body['request']['input'] == captured[0]['input']
    assert extras == {
      'turn_index': 0,
      'call_index': 1,
      'response_id': 'resp_xyz',
      'wire': 'responses',
    }

  @pytest.mark.asyncio
  async def test_records_only_canonical_response_and_tool_result_rows(self):
    gpt, tracker, captured = _make_responses_loop_with_tracker([_StaticTool('ping')])
    # first response: reasoning + interim assistant + tool_call → tool loop runs;
    # second response: terminal reasoning + terminal assistant.
    first = _fake_response(
      output=[
        _reasoning_item('thinking part 1', 'thinking part 2'),
        _message_item('checking…'),
        _function_call_item('ping', call_id='call_a'),
      ],
      response_id='resp_1',
    )
    second = _fake_response(
      output=[
        _reasoning_item('final thought'),
        _message_item('done'),
      ],
      response_id='resp_2',
    )
    _install_responses(gpt, [first, second], captured)

    await gpt.send([UserMessage((Text('go'),))])

    assert [step[0] for step in tracker.steps] == [
      'user_input',
      'llm_call',
      'tool_result',
      'llm_call',
    ]

  @pytest.mark.asyncio
  async def test_response_items_continue_to_drive_the_observer(self):
    gpt, _, captured = _make_responses_loop_with_tracker([_StaticTool('ping')])
    observer = MagicMock(spec=Observer)
    gpt.observer = observer
    first = _fake_response(
      output=[
        _reasoning_item('thinking'),
        _message_item('interim'),
        _function_call_item('ping', call_id='c1', arguments='{"x": 1}'),
      ],
    )
    second = _fake_response(output=[_message_item('final')])
    _install_responses(gpt, [first, second], captured)

    await gpt.send([UserMessage((Text('go'),))])

    assert observer.on_event.call_args_list == [
      call(ReasoningEvent('thinking')),
      call(InterimAssistantTextEvent('interim')),
      call(ToolCallEvent('c1', 'svc__ping', {'x': 1})),
      call(ToolResultEvent('c1', 'svc__ping', 'ok')),
    ]

  @pytest.mark.asyncio
  async def test_commentary_in_the_final_response_drives_the_observer(self):
    gpt, _, captured = _make_responses_loop_with_tracker()
    observer = MagicMock(spec=Observer)
    gpt.observer = observer
    terminal = _fake_response(
      output=[
        _reasoning_item('scoping'),
        _message_item(
          'migration rewrites 5,260 trails; (1) migrate or (2) stop', phase='commentary'
        ),
        _reasoning_item('asking'),
        _message_item('Waiting for your choice: (1) or (2).', phase='final_answer'),
      ]
    )
    _install_responses(gpt, [terminal], captured)

    reply = await gpt.send([UserMessage((Text('go'),))])

    assert reply == 'Waiting for your choice: (1) or (2).'
    assert observer.on_event.call_args_list == [
      call(ReasoningEvent('scoping')),
      call(InterimAssistantTextEvent('migration rewrites 5,260 trails; (1) migrate or (2) stop')),
      call(ReasoningEvent('asking')),
    ]

  @pytest.mark.asyncio
  async def test_executing_tool_exposes_its_llm_call_source(self):
    sources = []

    class _SourceTool(_StaticTool):
      async def call(self, arguments: dict):
        sources.append(gpt.tracker.current_tool_step_id)
        return 'ok'

    gpt, tracker, captured = _make_responses_loop_with_tracker([_SourceTool('ping')])
    first = _fake_response(
      output=[_reasoning_item('thinking'), _function_call_item('ping', call_id='c1')]
    )
    second = _fake_response(output=[_message_item('done')])
    _install_responses(gpt, [first, second], captured)

    await gpt.send([UserMessage((Text('go'),))])

    assert sources == [{'step_id': 1, 'index': 2}]
    assert tracker.current_tool_step_id is None

  @pytest.mark.asyncio
  async def test_turn_index_stays_on_the_user_turn_while_call_index_advances(self):
    gpt, tracker, captured = _make_responses_loop_with_tracker([_StaticTool('ping')])
    first = _fake_response(output=[_function_call_item('ping', call_id='c1')])
    second = _fake_response(output=[_function_call_item('ping', call_id='c2')])
    third = _fake_response(output=[_message_item('done')])
    _install_responses(gpt, [first, second, third], captured)

    await gpt.send([UserMessage((Text('go'),))])

    turns_by_kind: dict[str, list[int]] = {}
    for kind, _, extras in tracker.steps:
      turns_by_kind.setdefault(kind, []).append(extras['turn_index'])
    assert turns_by_kind == {
      'user_input': [0],
      'llm_call': [0, 0, 0],
      'tool_result': [0, 0],
    }
    assert [extras['call_index'] for kind, _, extras in tracker.steps if kind == 'llm_call'] == [
      1,
      2,
      3,
    ]

  @pytest.mark.asyncio
  async def test_subsequent_send_advances_turn_for_new_user_input(self):
    gpt, tracker, captured = _make_responses_loop_with_tracker()
    _install_responses(
      gpt,
      [
        _fake_response(output=[_message_item('hi')], response_id='r1'),
        _fake_response(output=[_message_item('bye')], response_id='r2'),
      ],
      captured,
    )

    await gpt.send([UserMessage((Text('first'),))])
    await gpt.send([UserMessage((Text('second'),))])

    user_turns = [extras['turn_index'] for kind, _, extras in tracker.steps if kind == 'user_input']
    llm_turns = [extras['turn_index'] for kind, _, extras in tracker.steps if kind == 'llm_call']
    call_indexes = [extras['call_index'] for kind, _, extras in tracker.steps if kind == 'llm_call']
    assert user_turns == [0, 1]
    assert llm_turns == [0, 1]
    assert call_indexes == [1, 2]


class _NewsTool(_StaticTool):
  def __init__(self, registry: Registry):
    super().__init__('news')
    self.registry = registry

  async def call(self, arguments: dict):
    job = self.registry.start('printf "background news\\n"', 'bg')
    await asyncio.to_thread(_wait_for_job, job)
    return 'tool done'


def _wait_for_job(job: Job) -> None:
  with job._condition:
    while not job._finished_locked():
      job._condition.wait()


class TestNotificationDelivery:
  @pytest.mark.asyncio
  async def test_tool_batch_appends_notification_after_function_outputs(self):
    inbox = Inbox()
    registry = Registry(inbox)
    tool = _NewsTool(registry)
    gpt, tracker, captured = _make_responses_loop_with_tracker([tool], inbox=inbox)
    observer = MagicMock(spec=Observer)
    gpt.observer = observer
    first = _fake_response(output=[_function_call_item('news', call_id='c1')])
    second = _fake_response(output=[_message_item('done')], response_id='r2')
    _install_responses(gpt, [first, second], captured)

    assert await gpt.send([UserMessage((Text('go'),))]) == 'done'

    [function_output, notification] = captured[1]['input']
    assert function_output['type'] == 'function_call_output'
    assert notification['role'] == 'user'
    assert 'background news' in notification['content']
    notification_steps = [step for step in tracker.steps if step[0] == 'notification']
    assert notification_steps == [
      (
        'notification',
        notification['content'],
        {'turn_index': 0, 'call_index': 1, 'job_ids': ['job-1']},
      )
    ]
    assert (
      call(NotificationEvent(notification['content'], ('job-1',)))
      in observer.on_event.call_args_list
    )

  @pytest.mark.asyncio
  async def test_pending_news_precedes_the_next_user_message(self):
    inbox = Inbox()
    gpt, tracker, captured = _make_responses_loop_with_tracker(inbox=inbox)
    _install_responses(
      gpt,
      [
        _fake_response(output=[_message_item('first')], response_id='r1'),
        _fake_response(output=[_message_item('second')], response_id='r2'),
      ],
      captured,
    )
    await gpt.send([UserMessage((Text('first'),))])
    inbox.post('[watch] between turns')

    await gpt.send([UserMessage((Text('second'),))])

    notification, user_message = captured[1]['input']
    assert notification['role'] == 'user'
    assert 'between turns' in notification['content']
    assert user_message['role'] == 'user'
    assert user_message['content'] == 'second'
    turn_steps = [
      (kind, extras['turn_index'])
      for kind, _, extras in tracker.steps
      if kind in {'notification', 'user_input'}
    ]
    assert turn_steps == [('user_input', 0), ('notification', 1), ('user_input', 1)]

  @pytest.mark.asyncio
  async def test_wake_starts_a_notification_only_turn(self):
    inbox = Inbox()
    gpt, tracker, captured = _make_responses_loop_with_tracker(inbox=inbox)
    _install_responses(
      gpt,
      [
        _fake_response(output=[_message_item('first')], response_id='r1'),
        _fake_response(output=[_message_item('noticed')], response_id='r2'),
      ],
      captured,
    )
    await gpt.send([UserMessage((Text('first'),))])
    inbox.post('[watch] wake')

    assert await gpt.wake() == 'noticed'

    assert len(captured[1]['input']) == 1
    assert captured[1]['input'][0]['role'] == 'user'
    notification_steps = [step for step in tracker.steps if step[0] == 'notification']
    assert notification_steps[0][2]['turn_index'] == 1
    assert [step for step in tracker.steps if step[0] == 'user_input'] == [
      ('user_input', 'first', {'turn_index': 0})
    ]

  @pytest.mark.asyncio
  async def test_wake_delivers_a_posted_notice_as_a_user_item_without_job_ids(self):
    inbox = Inbox()
    gpt, tracker, captured = _make_responses_loop_with_tracker(inbox=inbox)
    observer = MagicMock(spec=Observer)
    gpt.observer = observer
    _install_responses(
      gpt,
      [
        _fake_response(output=[_message_item('first')], response_id='r1'),
        _fake_response(output=[_message_item('settled')], response_id='r2'),
      ],
      captured,
    )
    await gpt.send([UserMessage((Text('first'),))])
    inbox.post('[notification: the framework]\nsettle the run')

    assert await gpt.wake() == 'settled'

    assert captured[1]['input'] == [
      {'role': 'user', 'content': '[notification: the framework]\nsettle the run'}
    ]
    assert [step for step in tracker.steps if step[0] == 'notification'] == [
      (
        'notification',
        '[notification: the framework]\nsettle the run',
        {'turn_index': 1, 'call_index': 2, 'job_ids': []},
      )
    ]
    assert (
      call(NotificationEvent('[notification: the framework]\nsettle the run', ()))
      in observer.on_event.call_args_list
    )

  @pytest.mark.asyncio
  async def test_interrupted_delivery_rides_pending_input_into_the_next_turn(self):
    inbox = Inbox()
    registry = Registry(inbox)
    tool = _NewsTool(registry)
    gpt, tracker, captured = _make_responses_loop_with_tracker([tool], inbox=inbox)
    first = _fake_response(output=[_function_call_item('news', call_id='c1')])
    followup_started = asyncio.Event()

    async def create(**kwargs):
      captured.append(kwargs)
      if len(captured) == 1:
        return first
      followup_started.set()
      await asyncio.Future()

    gpt.wire.client = cast(Any, SimpleNamespace(responses=SimpleNamespace(create=create)))
    turn = asyncio.create_task(gpt.send([UserMessage((Text('go'),))]))
    await followup_started.wait()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
      await turn

    resumed: list[dict] = []
    _install_responses(
      gpt, [_fake_response(output=[_message_item('resumed')], response_id='r2')], resumed
    )
    assert await gpt.send([UserMessage((Text('continue'),))]) == 'resumed'

    pending_output, pending_notification, user_message = resumed[0]['input']
    assert pending_output['type'] == 'function_call_output'
    assert pending_notification['role'] == 'user'
    assert 'background news' in pending_notification['content']
    assert user_message['content'] == 'continue'
    assert len([step for step in tracker.steps if step[0] == 'notification']) == 1


class TestParseResponse:
  def test_single_message_text_returned(self):
    response = _fake_response(output=[_message_item('hello')])
    assert parse_response(response.model_dump(mode='json')) == 'hello'

  def test_commentary_is_not_part_of_the_reply(self):
    response = _fake_response(
      output=[
        _message_item('closing the task out', phase='commentary'),
        _reasoning_item('finalizing'),
        _message_item('PR merged; task closed', phase='final_answer'),
      ]
    )
    assert parse_response(response.model_dump(mode='json')) == 'PR merged; task closed'

  def test_multiple_messages_without_phase_concatenated_in_order(self):
    response = _fake_response(output=[_message_item('part one'), _message_item('part two')])
    assert parse_response(response.model_dump(mode='json')) == 'part one\n\npart two'

  @pytest.mark.parametrize(
    'output',
    [
      [_message_item('', phase='final_answer')],
      [SimpleNamespace(type='message', phase=None, content=[])],
      [_message_item('still working', phase='commentary')],
      [_reasoning_item('only thinking')],
      [{'type': 'compaction', 'encrypted_content': 'opaque'}, _message_item('')],
      [],
    ],
    ids=[
      'empty-final-answer',
      'no-content',
      'commentary-alone',
      'reasoning-alone',
      'compaction',
      'no-output',
    ],
  )
  def test_an_answer_without_reply_text_is_empty(self, output):
    response = _fake_response(output=output)
    assert parse_response(response.model_dump(mode='json')) == ''

  @pytest.mark.parametrize(
    'item', [{'type': 'future_output'}, {'type': 'reasoning', 'summary': {}}]
  )
  def test_unrecognized_output_raises_without_reply_text(self, item):
    response = _fake_response(output=[item])
    with pytest.raises(RuntimeError, match='unreadable output without reply text'):
      parse_response(response.model_dump(mode='json'))
    beside_text = _fake_response(output=[item, _message_item('reply')])
    assert parse_response(beside_text.model_dump(mode='json')) == 'reply'

  @pytest.mark.parametrize(
    'part',
    [{'type': 'output_text', 'text': 123}, {'type': 'output_text'}, {'type': 'output_audio'}],
    ids=['non-string-text', 'missing-text', 'unknown-type'],
  )
  def test_unreadable_reply_content_raises(self, part):
    response = _fake_response(output=[SimpleNamespace(type='message', phase=None, content=[part])])
    with pytest.raises(RuntimeError, match='unreadable reply content'):
      parse_response(response.model_dump(mode='json'))

  def test_refusal_raises_with_refusal_text(self):
    refusal = SimpleNamespace(type='refusal', refusal='cannot help with that')
    response = _fake_response(
      output=[SimpleNamespace(type='message', phase=None, content=[refusal])]
    )
    with pytest.raises(RuntimeError, match='cannot help with that'):
      parse_response(response.model_dump(mode='json'))


class TestReplyExtractionFallback:
  # once the tool loop is done every side effect has landed, so send() must
  # not fail the run over a reply-extraction edge when terminal text exists.

  @pytest.mark.asyncio
  async def test_send_falls_back_to_message_text_when_extraction_raises(self):
    gpt, _, captured = _make_responses_loop_with_tracker()
    refusal = SimpleNamespace(type='refusal', refusal='cannot help with that')
    terminal = _fake_response(
      output=[
        _message_item('wrapping up', phase='commentary'),
        SimpleNamespace(type='message', phase=None, content=[refusal]),
        _message_item('the work landed'),
      ]
    )
    _install_responses(gpt, [terminal], captured)
    assert await gpt.send([UserMessage((Text('hi'),))]) == 'the work landed'

  @pytest.mark.asyncio
  @pytest.mark.parametrize(
    'part,error',
    [
      ({'type': 'refusal', 'refusal': 'cannot help with that'}, 'cannot help'),
      ({'type': 'output_text', 'text': 123}, 'unreadable reply content'),
      (None, 'unreadable output without reply text'),
    ],
    ids=['refusal-only', 'unreadable-text', 'unrecognized-item'],
  )
  async def test_unusable_terminal_response_is_recorded_and_accounted(
    self, monkeypatch, part, error
  ):
    gpt, tracker, captured = _make_responses_loop_with_tracker(agent='test-agent')
    terminal = _fake_response(
      output=[
        {'type': 'future_output'}
        if part is None
        else SimpleNamespace(type='message', phase=None, content=[part])
      ],
      model='billed-model',
      usage=_fake_usage(input_tokens=13, cached_tokens=4, output_tokens=7),
    )
    published = []
    monkeypatch.setattr(usage, 'publish', lambda agent, counts: published.append((agent, counts)))
    _install_responses(gpt, [terminal], captured)

    with pytest.raises(RuntimeError, match=error):
      await gpt.send([UserMessage((Text('hi'),))])

    assert [kind for kind, _, _ in tracker.steps] == ['user_input', 'llm_call']
    assert tracker.steps[-1][1]['response'] == terminal.model_dump(mode='json')
    totals = {'billed-model': {'input': 9, 'cache_write': 0, 'cache_read': 4, 'output': 7}}
    assert gpt.cumulative_usage() == totals
    assert published == [('test-agent', totals)]

  @pytest.mark.asyncio
  async def test_unusable_response_acknowledges_pending_notification_inputs(self):
    inbox = Inbox()
    inbox.post('notice')
    gpt, tracker, captured = _make_responses_loop_with_tracker(inbox=inbox)
    refusal = _fake_response(
      response_id='accepted',
      output=[
        SimpleNamespace(
          type='message',
          phase=None,
          content=[SimpleNamespace(type='refusal', refusal='cannot help')],
        )
      ],
    )
    _install_responses(
      gpt, [refusal, _fake_response(output=[_message_item('continued')])], captured
    )

    with pytest.raises(RuntimeError, match='cannot help'):
      await gpt.send([UserMessage((Text('hi'),))])
    assert await gpt.send([UserMessage((Text('continue'),))]) == 'continued'

    assert captured[0]['input'] == [
      {'role': 'user', 'content': 'notice'},
      {'role': 'user', 'content': 'hi'},
    ]
    assert 'stopped' not in next(extras for kind, _, extras in tracker.steps if kind == 'llm_call')
    assert captured[1]['previous_response_id'] == 'accepted'
    assert captured[1]['input'] == [{'role': 'user', 'content': 'continue'}]
    assert [kind for kind, _, _ in tracker.steps].count('notification') == 1


class TestReasoningKwargs:
  def test_include_added_when_reasoning_effort_set(self):
    gpt = make_loop(api_key='dummy', inbox=Inbox(), reasoning_effort='medium')
    kwargs = gpt.wire.recipe.request_options()
    assert kwargs['reasoning'] == {'effort': 'medium', 'summary': 'auto'}
    assert kwargs['include'] == ['reasoning.encrypted_content']

  def test_no_include_when_reasoning_effort_absent(self):
    gpt = make_loop(api_key='dummy', inbox=Inbox())
    assert gpt.wire.recipe.request_options() == {}

  @pytest.mark.asyncio
  async def test_send_passes_include_to_responses_create(self):
    gpt, _, captured = _make_responses_loop_with_tracker(reasoning_effort='medium')
    _install_responses(gpt, [_fake_response(output=[_message_item('hi')])], captured)
    await gpt.send([UserMessage((Text('go'),))])
    assert captured[0]['include'] == ['reasoning.encrypted_content']
    assert captured[0]['reasoning'] == {'effort': 'medium', 'summary': 'auto'}


class TestContextManagementKwargs:
  def test_kwargs_present_when_threshold_set(self):
    gpt = make_loop(api_key='dummy', inbox=Inbox(), compact_threshold=50_000)
    assert gpt.wire.recipe.request_options() == {
      'context_management': [{'type': 'compaction', 'compact_threshold': 50_000}]
    }

  def test_no_kwargs_when_threshold_absent(self):
    gpt = make_loop(api_key='dummy', inbox=Inbox())
    assert gpt.wire.recipe.request_options() == {}

  @pytest.mark.asyncio
  async def test_send_passes_context_management_on_every_create(self):
    # the tool loop's follow-up create must carry the param too — compaction
    # most plausibly triggers mid-loop, where the context grows fastest.
    gpt, _, captured = _make_responses_loop_with_tracker(
      [_StaticTool('ping')], compact_threshold=1_000
    )
    first = _fake_response(output=[_function_call_item('ping', call_id='c1')])
    second = _fake_response(output=[_message_item('done')])
    _install_responses(gpt, [first, second], captured)

    await gpt.send([UserMessage((Text('go'),))])

    expected = [{'type': 'compaction', 'compact_threshold': 1_000}]
    assert [kwargs['context_management'] for kwargs in captured] == [expected, expected]

  @pytest.mark.asyncio
  async def test_send_omits_context_management_when_disabled(self):
    gpt, _, captured = _make_responses_loop_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('hi')])], captured)

    await gpt.send([UserMessage((Text('go'),))])

    assert 'context_management' not in captured[0]


class TestUsageAccounting:
  @pytest.mark.asyncio
  async def test_cumulative_usage_maps_openai_classes(self):
    # reasoning tokens sit inside output_tokens, not beside it.
    gpt, _, captured = _make_responses_loop_with_tracker()
    response = _fake_response(
      output=[_message_item('ok')],
      usage=_fake_usage(input_tokens=100, output_tokens=22, cached_tokens=30),
    )
    _install_responses(gpt, [response], captured)

    await gpt.send([UserMessage((Text('hi'),))])

    assert gpt.cumulative_usage() == {
      'gpt-5': {'input': 70, 'cache_write': 0, 'cache_read': 30, 'output': 22}
    }

  @pytest.mark.asyncio
  async def test_cumulative_usage_credits_cache_writes(self):
    gpt, _, captured = _make_responses_loop_with_tracker()
    response = _fake_response(
      output=[_message_item('ok')],
      usage=_fake_usage(
        input_tokens=8_687, output_tokens=190, cached_tokens=0, cache_write_tokens=8_684
      ),
    )
    _install_responses(gpt, [response], captured)

    await gpt.send([UserMessage((Text('hi'),))])

    assert gpt.cumulative_usage() == {
      'gpt-5': {'input': 3, 'cache_write': 8_684, 'cache_read': 0, 'output': 190}
    }

  @pytest.mark.asyncio
  async def test_cumulative_usage_sums_across_calls_keyed_by_response_model(self):
    gpt, _, captured = _make_responses_loop_with_tracker([_StaticTool('ping')])
    first = _fake_response(
      output=[_function_call_item('ping', call_id='c1')],
      usage=_fake_usage(input_tokens=10, output_tokens=5, cached_tokens=0),
      model='gpt-5-2025-08-07',
    )
    second = _fake_response(
      output=[_message_item('done')],
      usage=_fake_usage(input_tokens=40, output_tokens=6, cached_tokens=25),
      model='gpt-5-2025-08-07',
    )
    _install_responses(gpt, [first, second], captured)

    await gpt.send([UserMessage((Text('go'),))])

    assert gpt.cumulative_usage() == {
      'gpt-5-2025-08-07': {'input': 25, 'cache_write': 0, 'cache_read': 25, 'output': 11}
    }

  @pytest.mark.asyncio
  async def test_agent_publishes_usage_file_after_every_call(self, tmp_path, monkeypatch):
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(tmp_path))
    gpt, _, captured = _make_responses_loop_with_tracker([_StaticTool('ping')], agent='bro//dev')
    first = _fake_response(
      output=[_function_call_item('ping', call_id='c1')],
      usage=_fake_usage(input_tokens=10, output_tokens=5, cached_tokens=0),
    )
    second = _fake_response(
      output=[_message_item('done')],
      usage=_fake_usage(input_tokens=20, output_tokens=7, cached_tokens=4),
    )
    _install_responses(gpt, [first, second], captured)

    await gpt.send([UserMessage((Text('go'),))])

    published = usage.current_usage()
    assert published is not None
    assert published.agent == 'bro//dev'
    assert published.per_model == {
      'gpt-5': {'input': 26, 'cache_write': 0, 'cache_read': 4, 'output': 12}
    }

  @pytest.mark.asyncio
  async def test_no_agent_publishes_nothing(self, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    gpt, _, captured = _make_responses_loop_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ok')])], captured)

    await gpt.send([UserMessage((Text('hi'),))])

    # publishing is gated on the agent identity; without it no pointer is minted
    assert usage.USAGE_FILE_VARIABLE not in os.environ


class _BlockingTool(Tool):
  """a tool that never returns on its own — the interruption fixture."""

  def __init__(self, name: str):
    self._name = name
    self.started = asyncio.Event()

  @property
  def name(self) -> str:
    return self._name

  @property
  def description(self) -> str:
    return 'blocks'

  @property
  def parameters(self) -> dict:
    return {'type': 'object', 'properties': {}}

  async def call(self, arguments: dict):
    self.started.set()
    await asyncio.Event().wait()
    raise AssertionError('unreachable')


def _tool_calls_response(*names: str) -> Response:
  return _fake_response(
    response_id='resp_tools',
    output=[
      _function_call_item(name, call_id=f'call_{index}')
      for index, name in enumerate(names, start=1)
    ],
  )


class TestInterruptedTurn:
  @pytest.mark.asyncio
  async def test_pending_tool_calls_are_answered_and_parked(self):
    blocking = _BlockingTool('slow')
    gpt, tracker, captured = _make_responses_loop_with_tracker([blocking, _StaticTool('quick')])
    _install_responses(
      gpt,
      [_tool_calls_response('slow', 'quick'), _fake_response(output=[_message_item('after')])],
      captured,
    )

    turn = asyncio.create_task(gpt.send([UserMessage((Text('go'),))]))
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
      await turn

    # both the call that was running and the one that never started come back
    # answered, so the next request is a turn the API accepts
    parked = [cast(ToolOutput, item) for item in gpt._pending_input]
    assert [item.call_id for item in parked] == ['call_1', 'call_2']
    assert all(item.output == loop_module.INTERRUPTED_TOOL_OUTPUT for item in parked)
    # the trail records them too — a replay reconstructs the same turn
    interrupted = [step for step in tracker.steps if step[1] == loop_module.INTERRUPTED_TOOL_OUTPUT]
    assert [step[0] for step in interrupted] == ['tool_result', 'tool_result']
    assert all(step[2]['is_error'] is True for step in interrupted)

    reply = await gpt.send([UserMessage((Text('never mind, do this'),))])
    assert reply == 'after'
    # the resumed request chains on the response that made the calls and leads
    # with their answers, then the new message
    resumed = captured[-1]
    assert resumed['previous_response_id'] == 'resp_tools'
    assert [item.get('call_id') for item in resumed['input'][:2]] == ['call_1', 'call_2']
    assert resumed['input'][2]['content'] == 'never mind, do this'
    assert gpt._pending_input == []

  @pytest.mark.asyncio
  async def test_unanswered_request_is_parked_whole(self):
    gpt, _tracker, captured = _make_responses_loop_with_tracker()
    started = asyncio.Event()

    async def create(**kwargs):
      captured.append(kwargs)
      started.set()
      await asyncio.Event().wait()

    gpt.wire.client = cast(Any, SimpleNamespace(responses=SimpleNamespace(create=create)))

    turn = asyncio.create_task(gpt.send([UserMessage((Text('go'),))]))
    await asyncio.wait_for(started.wait(), timeout=5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
      await turn

    # no response came back, so the whole request rides into the next send
    # rather than vanishing from the conversation
    assert gpt._pending_input == [UserMessage((Text('go'),))]
    assert gpt.wire._last_response_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{broken', '[]', 'null', '1', '"text"', '', '  '])
async def test_argument_errors_are_returned_to_the_model_without_calling_the_tool(arguments):
  tool = _StaticTool('ping')
  invoked = []

  async def invoke(arguments):
    invoked.append(arguments)
    return 'ok'

  tool.call = invoke
  loop, tracker, captured = _make_responses_loop_with_tracker([tool])
  _install_responses(
    loop,
    [
      _fake_response(output=[_function_call_item('ping', call_id='c1', arguments=arguments)]),
      _fake_response(output=[_message_item('recovered')]),
    ],
    captured,
  )

  assert await loop.send([UserMessage((Text('go'),))]) == 'recovered'

  empty = not arguments.strip()
  assert invoked == ([{}] if empty else [])
  output = captured[1]['input'][0]['output']
  assert output == ('ok' if empty else "tool 'svc__ping' failed: arguments are not a JSON object")
  result = next(step for step in tracker.steps if step[0] == 'tool_result')
  assert result[2]['is_error'] is not empty


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['llm_call', 'tool_result', 'user_input'])
async def test_cancellation_during_recording_keeps_the_conversation_answerable(kind):
  loop, tracker, captured = _make_responses_loop_with_tracker([_StaticTool('ping')])
  entered = asyncio.Event()
  release = threading.Event()
  event_loop = asyncio.get_running_loop()
  original_step = tracker.step
  blocked_kind = kind

  def step(kind, body, **extras):
    result = original_step(kind, body, **extras)
    if kind == blocked_kind and not release.is_set():
      event_loop.call_soon_threadsafe(entered.set)
      assert release.wait(5), 'test did not release recording'
    return result

  tracker.step = step
  _install_responses(
    loop,
    [_tool_calls_response('ping', 'ping'), _fake_response(output=[_message_item('continued')])],
    captured,
  )
  turn = asyncio.create_task(loop.send([UserMessage((Text('go'),))]))
  with ExitStack() as cleanup:
    cleanup.callback(release.set)
    await entered.wait()
    turn.cancel()
  with pytest.raises(asyncio.CancelledError):
    await turn

  if kind == 'user_input':
    _install_responses(loop, [_fake_response(output=[_message_item('continued')])], captured)
  assert await loop.send([UserMessage((Text('continue'),))]) == 'continued'
  if kind == 'user_input':
    assert [item['content'] for item in captured[0]['input']] == ['go', 'continue']
  else:
    outputs = captured[-1]['input'][:-1]
    assert [item['call_id'] for item in outputs] == ['call_1', 'call_2']
    assert outputs[0]['output'] == (
      'ok' if kind == 'tool_result' else loop_module.INTERRUPTED_TOOL_OUTPUT
    )
    assert outputs[1]['output'] == loop_module.INTERRUPTED_TOOL_OUTPUT
    results = [step for step in tracker.steps if step[0] == 'tool_result']
    assert [step[2]['call_id'] for step in results] == ['call_1', 'call_2']
    assert results[-1][2]['is_error'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'reason, stop',
  [('max_output_tokens', 'truncated'), ('content_filter', 'filtered'), ('other', 'failed')],
)
@pytest.mark.parametrize('output_kind', ['text', 'calls', 'empty'])
async def test_stopped_legs_are_recorded_counted_and_refused_before_events(
  reason, stop, output_kind, monkeypatch
):
  loop, tracker, captured = _make_responses_loop_with_tracker(
    [_StaticTool('ping')], agent='test-agent'
  )
  observer = MagicMock(spec=Observer)
  loop.observer = observer
  output = {
    'text': [_message_item('partial')],
    'calls': [_function_call_item('ping', call_id='c1')],
    'empty': [],
  }[output_kind]
  stopped = _fake_response(
    output=output,
    response_id='refused',
    dump_payload={'status': 'incomplete', 'incomplete_details': {'reason': reason}},
  )
  published = []
  monkeypatch.setattr(
    usage, 'publish', lambda agent, counts: published.append((agent, counts.copy()))
  )
  _install_responses(loop, [stopped, _fake_response(output=[_message_item('continued')])], captured)

  with pytest.raises(LLMStopped, match=reason) as error:
    await loop.send([SystemPrompt('prompt'), UserMessage((Text('go'),))])

  assert error.value.reason == stop
  assert error.value.stop_reason == reason
  assert [step[0] for step in tracker.steps] == ['user_input', 'llm_call']
  assert tracker.steps[-1][2]['stopped'] == stop
  assert tracker.steps[-1][1]['response'] == stopped.model_dump(mode='json')
  assert len(published) == 1
  assert published[0][1]['gpt-5']['output'] == 20
  observer.on_event.assert_not_called()
  assert loop.wire._last_response_id is None
  assert await loop.send([UserMessage((Text('continue'),))]) == 'continued'
  assert 'previous_response_id' not in captured[1]
  assert [item['content'] for item in captured[1]['input']] == ['prompt', 'go', 'continue']


@pytest.mark.asyncio
async def test_cancelled_capacity_wait_keeps_inputs_without_answering_refused_calls(monkeypatch):
  from dataclasses import replace

  from bro.llm import retry as llm_retry

  loop, tracker, captured = _make_responses_loop_with_tracker()
  observer = MagicMock(spec=Observer)
  loop.observer = observer
  waiting = asyncio.Event()

  async def pause(seconds):
    waiting.set()
    await asyncio.Future()

  async def exchange(inputs, tools, request_timeout):
    return replace(_function_call_response('ping'), stop='capacity', stop_reason='resources')

  original_exchange = loop.wire.exchange
  monkeypatch.setattr(loop.wire, 'exchange', exchange)
  monkeypatch.setattr(llm_retry, 'asyncio_sleep', pause)
  turn = asyncio.create_task(loop.send([UserMessage((Text('go'),))]))
  await waiting.wait()
  turn.cancel()
  with pytest.raises(asyncio.CancelledError):
    await turn
  assert [step[0] for step in tracker.steps] == ['user_input', 'llm_call']
  assert tracker.steps[-1][2]['stopped'] == 'capacity'
  observer.on_event.assert_not_called()
  monkeypatch.setattr(loop.wire, 'exchange', original_exchange)
  _install_responses(loop, [_fake_response(output=[_message_item('continued')])], captured)
  assert await loop.send([UserMessage((Text('continue'),))]) == 'continued'
  assert [item['content'] for item in captured[0]['input']] == ['go', 'continue']


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_answering_the_received_calls(monkeypatch):
  loop, tracker, captured = _make_responses_loop_with_tracker([_StaticTool('ping')])
  leg_recording = asyncio.Event()
  result_recording = asyncio.Event()
  release_leg = asyncio.Event()
  release_result = asyncio.Event()
  original_track = loop._track_step

  async def track(kind, body, **extras):
    if kind == 'llm_call' and not release_leg.is_set():
      leg_recording.set()
      await release_leg.wait()
    if kind == 'tool_result' and not release_result.is_set():
      result_recording.set()
      await release_result.wait()
    return await original_track(kind, body, **extras)

  monkeypatch.setattr(loop, '_track_step', track)
  _install_responses(loop, [_tool_calls_response('ping', 'ping')], captured)
  turn = asyncio.create_task(loop.send([UserMessage((Text('go'),))]))
  with ExitStack() as cleanup:
    cleanup.callback(release_leg.set)
    cleanup.callback(release_result.set)
    await leg_recording.wait()
    turn.cancel()
    release_leg.set()
    await result_recording.wait()
    turn.cancel()
    release_result.set()
    with pytest.raises(asyncio.CancelledError):
      await turn
  results = [step for step in tracker.steps if step[0] == 'tool_result']
  assert [step[2]['call_id'] for step in results] == ['call_1', 'call_2']
  assert [item.call_id for item in cast(list[ToolOutput], loop._pending_input)] == [
    'call_1',
    'call_2',
  ]
