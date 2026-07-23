import os
from types import SimpleNamespace
from typing import Any, Optional, cast

import pytest
from openai.types.responses import Response

import llm.llm
import llm.usage as usage
from llm.llms.chat_gpt import ChatGPT, LLMSpec, parse_response
from llm.mcp import InProcessMCPServer, Tool, ToolControlSignal, ToolRegistry, wire_name
from llm.tracker import Tracker

# the registry advertises namespaced wire names, so a tool whose local name is
# `ping` in this namespace surfaces to the LLM as `svc__ping`. the emit helpers
# below wrap the local name the same way, modeling what the model calls back.
_TEST_NAMESPACE = 'svc'


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

  async def call(self, arguments: dict):
    if self._raise_with is not None:
      raise self._raise_with
    return 'ok'


def _function_call_response(name: str) -> Response:
  # SimpleNamespace duck-types the few fields _execute_tool_calls reads
  # (output[*].type / .name / .arguments / .call_id); cast keeps pyright quiet.
  return cast(
    Response,
    SimpleNamespace(
      output=[
        SimpleNamespace(
          type='function_call',
          name=wire_name(_TEST_NAMESPACE, name),
          arguments='{}',
          call_id='call_1',
        )
      ]
    ),
  )


def _make_chat_gpt(tools: list[Tool]) -> ChatGPT:
  gpt = ChatGPT(api_key='dummy')
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

  def step(self, kind, body, **extras) -> None:
    self.steps.append((kind, body, extras))

  def end_trail(self, reason, detail=None) -> None:
    self.ended.append(reason)


def _fake_usage(*, input_tokens=10, output_tokens=20, reasoning_tokens=5, cached_tokens=0):
  return SimpleNamespace(
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
  )


def _fake_response(
  *, output, response_id='resp_1', usage=None, dump_payload=None, model='gpt-5'
) -> Response:
  # `output` is the list of duck-typed items _emit_response_steps walks.
  # `dump_payload` becomes the response.model_dump() result captured into
  # the llm_call body — tests can pass a sentinel dict to verify it lands.
  namespace = SimpleNamespace(
    id=response_id,
    output=output,
    usage=usage if usage is not None else _fake_usage(),
    model=model,
  )
  payload = dump_payload if dump_payload is not None else {'id': response_id}
  namespace.model_dump = lambda mode='json': payload
  return cast(Response, namespace)


def _reasoning_item(*texts: str):
  return SimpleNamespace(
    type='reasoning',
    summary=[SimpleNamespace(type='summary_text', text=t) for t in texts],
  )


def _message_item(text: str, *, phase: Optional[str] = None):
  # the default carries no `phase` attribute at all, matching the SDK's
  # ResponseOutputMessage; gpt-5.6 payloads add it as an extra field.
  item = SimpleNamespace(
    type='message',
    content=[SimpleNamespace(type='output_text', text=text)],
  )
  if phase is not None:
    item.phase = phase
  return item


def _function_call_item(name: str, *, call_id: str, arguments='{}'):
  return SimpleNamespace(
    type='function_call',
    name=wire_name(_TEST_NAMESPACE, name),
    arguments=arguments,
    call_id=call_id,
  )


def _make_chat_gpt_with_tracker(
  tools: Optional[list[Tool]] = None,
  *,
  reasoning_effort=None,
  compact_threshold: Optional[int] = None,
  agent: Optional[str] = None,
) -> tuple[ChatGPT, _RecordingTracker, list[dict]]:
  """build a ChatGPT instance with mocked tool registry + tracker + a captured-
  kwargs sink for responses.create. callers wire `gpt.client.responses.create`
  to whatever stub sequence they need.
  """
  gpt = ChatGPT(
    api_key='dummy',
    reasoning_effort=reasoning_effort,
    compact_threshold=compact_threshold,
    agent=agent,
  )
  gpt.tools = ToolRegistry(
    [InProcessMCPServer(_TEST_NAMESPACE, tools)] if tools is not None else []
  )
  # bypass the real openai schema conversion path — tests don't care about it.
  gpt._openai_tools = []
  tracker = _RecordingTracker()
  gpt.tracker = tracker
  captured: list[dict] = []
  return gpt, tracker, captured


def _install_responses(gpt: ChatGPT, sequence: list, captured: list[dict]) -> None:
  iterator = iter(sequence)

  def create(**kwargs):
    captured.append(kwargs)
    return next(iterator)

  # OpenAI client exposes `responses` as a cached_property — write through to
  # a duck-typed namespace instead by replacing `gpt.client` wholesale. cast
  # keeps pyright quiet since the attribute is typed as the OpenAI class.
  gpt.client = cast(Any, SimpleNamespace(responses=SimpleNamespace(create=create)))


@pytest.mark.asyncio
async def test_tool_exception_becomes_function_call_output():
  tool = _StaticTool('boom', raise_with=RuntimeError('upstream down'))
  gpt = _make_chat_gpt([tool])

  results = await gpt._execute_tool_calls(_function_call_response('boom'), turn_index=1)

  assert len(results) == 1
  result = cast(dict, results[0])
  assert result['type'] == 'function_call_output'
  assert result['call_id'] == 'call_1'
  assert "'svc__boom' failed" in result['output']
  assert 'RuntimeError' in result['output']
  assert 'upstream down' in result['output']


@pytest.mark.asyncio
async def test_tool_control_signal_propagates_past_loop():
  class _Abort(ToolControlSignal):
    pass

  tool = _StaticTool('halt', raise_with=_Abort('stop the run'))
  gpt = _make_chat_gpt([tool])

  with pytest.raises(_Abort, match='stop the run'):
    await gpt._execute_tool_calls(_function_call_response('halt'), turn_index=1)


@pytest.mark.asyncio
async def test_successful_tool_call_returns_output_unchanged():
  gpt = _make_chat_gpt([_StaticTool('ping')])

  results = await gpt._execute_tool_calls(_function_call_response('ping'), turn_index=1)

  assert len(results) == 1
  assert cast(dict, results[0])['output'] == 'ok'


class TestToolResultTrackerEmission:
  @pytest.mark.asyncio
  async def test_emits_tool_result_with_call_id_and_is_error_false(self):
    gpt, tracker, _ = _make_chat_gpt_with_tracker([_StaticTool('ping')])
    await gpt._execute_tool_calls(_function_call_response('ping'), turn_index=2)

    results = [s for s in tracker.steps if s[0] == 'tool_result']
    assert len(results) == 1
    kind, body, extras = results[0]
    assert body == 'ok'
    assert extras == {
      'turn_index': 2,
      'tool_name': 'svc__ping',
      'call_id': 'call_1',
      'is_error': False,
    }

  @pytest.mark.asyncio
  async def test_emits_tool_result_with_is_error_true_on_exception(self):
    tool = _StaticTool('boom', raise_with=RuntimeError('upstream down'))
    gpt, tracker, _ = _make_chat_gpt_with_tracker([tool])
    await gpt._execute_tool_calls(_function_call_response('boom'), turn_index=3)

    results = [s for s in tracker.steps if s[0] == 'tool_result']
    assert len(results) == 1
    _, body, extras = results[0]
    assert "'svc__boom' failed" in body
    assert extras['is_error'] is True
    assert extras['turn_index'] == 3
    assert extras['call_id'] == 'call_1'


class TestSendTrackerEmission:
  @pytest.mark.asyncio
  async def test_user_input_emitted_at_turn_zero_skipping_system(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('hi back')])], captured)

    await gpt.send(
      [
        {'role': 'system', 'content': 'be helpful'},
        {'role': 'user', 'content': 'hello'},
      ]
    )

    user_steps = [s for s in tracker.steps if s[0] == 'user_input']
    assert len(user_steps) == 1
    _, body, extras = user_steps[0]
    assert body == 'hello'
    assert extras == {'turn_index': 0}

  @pytest.mark.asyncio
  async def test_user_input_text_extracted_from_multimodal_content(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ack')])], captured)

    await gpt.send(
      [
        {
          'role': 'user',
          'content': [
            {'type': 'text', 'text': 'caption this'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
          ],
        }
      ]
    )

    user_steps = [s for s in tracker.steps if s[0] == 'user_input']
    assert user_steps[0][1] == 'caption this'

  @pytest.mark.asyncio
  async def test_request_timeout_forwarded_to_responses_create(self):
    gpt, _, captured = _make_chat_gpt_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ok')])], captured)

    await gpt.send([{'role': 'user', 'content': 'hi'}], request_timeout=120.0)

    assert captured[0]['timeout'] == 120.0

  @pytest.mark.asyncio
  async def test_request_timeout_omitted_leaves_client_default(self):
    gpt, _, captured = _make_chat_gpt_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ok')])], captured)

    await gpt.send([{'role': 'user', 'content': 'hi'}])

    assert 'timeout' not in captured[0]

  @pytest.mark.asyncio
  async def test_llm_call_records_request_response_id_and_token_counts(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker()
    response = _fake_response(
      output=[_message_item('reply')],
      response_id='resp_xyz',
      usage=_fake_usage(input_tokens=11, output_tokens=22, reasoning_tokens=33, cached_tokens=7),
      dump_payload={'id': 'resp_xyz', 'output': ['…']},
    )
    _install_responses(gpt, [response], captured)

    await gpt.send([{'role': 'user', 'content': 'hi'}])

    llm_calls = [s for s in tracker.steps if s[0] == 'llm_call']
    assert len(llm_calls) == 1
    _, body, extras = llm_calls[0]
    assert body['response'] == {'id': 'resp_xyz', 'output': ['…']}
    assert body['request']['model'] == 'gpt-5'
    # request kwargs round-tripped, including the input list passed to the API.
    assert body['request']['input'] == captured[0]['input']
    assert extras == {
      'turn_index': 1,
      'response_id': 'resp_xyz',
      'tokens_in': 11,
      'tokens_out': 22,
      'tokens_reasoning': 33,
      'tokens_cached': 7,
    }

  @pytest.mark.asyncio
  async def test_emits_reasoning_assistant_tool_call_in_order(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')])
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

    await gpt.send([{'role': 'user', 'content': 'go'}])

    kinds = [s[0] for s in tracker.steps]
    # ordering: user_input, then per-output for first response (in source order),
    # then tool_result, then per-output for second response.
    assert kinds == [
      'user_input',
      'llm_call',
      'reasoning',
      'reasoning',
      'assistant',
      'tool_call',
      'tool_result',
      'llm_call',
      'reasoning',
      'assistant',
    ]

  @pytest.mark.asyncio
  async def test_assistant_carries_terminal_flag(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')])
    first = _fake_response(
      output=[_message_item('interim'), _function_call_item('ping', call_id='c1')],
    )
    second = _fake_response(output=[_message_item('final')])
    _install_responses(gpt, [first, second], captured)

    await gpt.send([{'role': 'user', 'content': 'go'}])

    assistants = [s for s in tracker.steps if s[0] == 'assistant']
    assert [(body, extras['terminal']) for _, body, extras in assistants] == [
      ('interim', False),
      ('final', True),
    ]

  @pytest.mark.asyncio
  async def test_tool_call_step_carries_name_args_and_call_id(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')])
    first = _fake_response(
      output=[_function_call_item('ping', call_id='c1', arguments='{"x": 1}')],
    )
    second = _fake_response(output=[_message_item('done')])
    _install_responses(gpt, [first, second], captured)

    await gpt.send([{'role': 'user', 'content': 'go'}])

    tool_calls = [s for s in tracker.steps if s[0] == 'tool_call']
    assert len(tool_calls) == 1
    _, body, extras = tool_calls[0]
    assert body is None
    assert extras['tool_name'] == 'svc__ping'
    assert extras['arguments'] == {'x': 1}
    assert extras['call_id'] == 'c1'

  @pytest.mark.asyncio
  async def test_turn_index_monotonic_across_responses(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')])
    first = _fake_response(output=[_function_call_item('ping', call_id='c1')])
    second = _fake_response(output=[_function_call_item('ping', call_id='c2')])
    third = _fake_response(output=[_message_item('done')])
    _install_responses(gpt, [first, second, third], captured)

    await gpt.send([{'role': 'user', 'content': 'go'}])

    turns_by_kind: dict[str, list[int]] = {}
    for kind, _, extras in tracker.steps:
      turns_by_kind.setdefault(kind, []).append(extras['turn_index'])
    assert turns_by_kind['user_input'] == [0]
    assert turns_by_kind['llm_call'] == [1, 2, 3]
    # tool_call belongs to the turn whose responses.create produced it; tool_result
    # rides on the same turn since it's the immediate response to that call.
    assert turns_by_kind['tool_call'] == [1, 2]
    assert turns_by_kind['tool_result'] == [1, 2]

  @pytest.mark.asyncio
  async def test_subsequent_send_advances_turn_for_new_user_input(self):
    gpt, tracker, captured = _make_chat_gpt_with_tracker()
    _install_responses(
      gpt,
      [
        _fake_response(output=[_message_item('hi')], response_id='r1'),
        _fake_response(output=[_message_item('bye')], response_id='r2'),
      ],
      captured,
    )

    await gpt.send([{'role': 'user', 'content': 'first'}])
    await gpt.send([{'role': 'user', 'content': 'second'}])

    user_turns = [extras['turn_index'] for k, _, extras in tracker.steps if k == 'user_input']
    llm_turns = [extras['turn_index'] for k, _, extras in tracker.steps if k == 'llm_call']
    # first user_input at 0, first llm_call at 1; subsequent send bumps off the
    # previous final turn (1) before emitting the next user_input at 2, then
    # llm_call at 3 — so user_input and llm_call never collide on a turn_index.
    assert user_turns == [0, 2]
    assert llm_turns == [1, 3]


class TestParseResponse:
  def test_single_message_text_returned(self):
    response = _fake_response(output=[_message_item('hello')])
    assert parse_response(response) == 'hello'

  def test_final_answer_phase_preferred_over_commentary(self):
    # the gpt-5.6 multi-message shape: a commentary-phase progress note, a
    # reasoning item, then the final_answer-phase reply
    response = _fake_response(
      output=[
        _message_item('closing the task out', phase='commentary'),
        _reasoning_item('finalizing'),
        _message_item('PR merged; task closed', phase='final_answer'),
      ]
    )
    assert parse_response(response) == 'PR merged; task closed'

  def test_multiple_messages_without_phase_concatenated_in_order(self):
    response = _fake_response(output=[_message_item('part one'), _message_item('part two')])
    assert parse_response(response) == 'part one\n\npart two'

  def test_no_message_items_raises(self):
    response = _fake_response(output=[_reasoning_item('only thinking')])
    with pytest.raises(RuntimeError, match="doesn't contain output messages"):
      parse_response(response)

  def test_messages_without_text_raise(self):
    response = _fake_response(output=[SimpleNamespace(type='message', content=[])])
    with pytest.raises(RuntimeError, match='no output texts'):
      parse_response(response)

  def test_refusal_raises_with_refusal_text(self):
    refusal = SimpleNamespace(type='refusal', refusal='cannot help with that')
    response = _fake_response(output=[SimpleNamespace(type='message', content=[refusal])])
    with pytest.raises(RuntimeError, match='cannot help with that'):
      parse_response(response)


class TestReplyExtractionFallback:
  # once the tool loop is done every side effect has landed, so send() must
  # not fail the run over a reply-extraction edge when terminal text exists.

  @pytest.mark.asyncio
  async def test_send_falls_back_to_message_text_when_extraction_raises(self):
    gpt, _, captured = _make_chat_gpt_with_tracker()
    refusal = SimpleNamespace(type='refusal', refusal='cannot help with that')
    terminal = _fake_response(
      output=[
        SimpleNamespace(type='message', content=[refusal]),
        _message_item('the work landed'),
      ]
    )
    _install_responses(gpt, [terminal], captured)
    assert await gpt.send([{'role': 'user', 'content': 'hi'}]) == 'the work landed'

  @pytest.mark.asyncio
  async def test_send_raises_when_terminal_response_has_no_text(self):
    gpt, _, captured = _make_chat_gpt_with_tracker()
    refusal = SimpleNamespace(type='refusal', refusal='cannot help with that')
    terminal = _fake_response(output=[SimpleNamespace(type='message', content=[refusal])])
    _install_responses(gpt, [terminal], captured)
    with pytest.raises(RuntimeError, match='cannot help with that'):
      await gpt.send([{'role': 'user', 'content': 'hi'}])


class TestReasoningKwargs:
  def test_include_added_when_reasoning_effort_set(self):
    gpt = ChatGPT(api_key='dummy', reasoning_effort='medium')
    kwargs = gpt._reasoning_kwargs()
    assert kwargs['reasoning'] == {'effort': 'medium', 'summary': 'auto'}
    assert kwargs['include'] == ['reasoning.encrypted_content']

  def test_no_include_when_reasoning_effort_absent(self):
    gpt = ChatGPT(api_key='dummy')
    assert gpt._reasoning_kwargs() == {}

  @pytest.mark.asyncio
  async def test_send_passes_include_to_responses_create(self):
    gpt, _, captured = _make_chat_gpt_with_tracker(reasoning_effort='medium')
    _install_responses(gpt, [_fake_response(output=[_message_item('hi')])], captured)
    await gpt.send([{'role': 'user', 'content': 'go'}])
    assert captured[0]['include'] == ['reasoning.encrypted_content']
    assert captured[0]['reasoning'] == {'effort': 'medium', 'summary': 'auto'}


class TestContextManagementKwargs:
  def test_kwargs_present_when_threshold_set(self):
    gpt = ChatGPT(api_key='dummy', compact_threshold=50_000)
    assert gpt._context_management_kwargs() == {
      'context_management': [{'type': 'compaction', 'compact_threshold': 50_000}]
    }

  def test_no_kwargs_when_threshold_absent(self):
    gpt = ChatGPT(api_key='dummy')
    assert gpt._context_management_kwargs() == {}

  @pytest.mark.asyncio
  async def test_send_passes_context_management_on_every_create(self):
    # the tool loop's follow-up create must carry the param too — compaction
    # most plausibly triggers mid-loop, where the context grows fastest.
    gpt, _, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')], compact_threshold=1_000)
    first = _fake_response(output=[_function_call_item('ping', call_id='c1')])
    second = _fake_response(output=[_message_item('done')])
    _install_responses(gpt, [first, second], captured)

    await gpt.send([{'role': 'user', 'content': 'go'}])

    expected = [{'type': 'compaction', 'compact_threshold': 1_000}]
    assert [kwargs['context_management'] for kwargs in captured] == [expected, expected]

  @pytest.mark.asyncio
  async def test_send_omits_context_management_when_disabled(self):
    gpt, _, captured = _make_chat_gpt_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('hi')])], captured)

    await gpt.send([{'role': 'user', 'content': 'go'}])

    assert 'context_management' not in captured[0]


class TestLLMSpec:
  def test_default_spec_has_no_optional_knobs(self):
    spec = LLMSpec()
    assert spec.model == 'gpt-5'
    assert spec.reasoning_effort is None
    assert spec.service_tier is None
    assert spec.compact_threshold is None

  def test_invalid_service_tier_rejected(self):
    with pytest.raises(ValueError, match='invalid service_tier'):
      LLMSpec(service_tier='nope')  # type: ignore[arg-type]

  def test_invalid_reasoning_effort_rejected(self):
    with pytest.raises(ValueError, match='invalid reasoning_effort'):
      LLMSpec(reasoning_effort='ludicrous')  # type: ignore[arg-type]

  @pytest.mark.parametrize('threshold', [0, -1])
  def test_invalid_compact_threshold_rejected(self, threshold: int):
    with pytest.raises(ValueError, match='invalid compact_threshold'):
      LLMSpec(compact_threshold=threshold)

  def test_fast_returns_new_spec_with_priority_tier(self):
    spec = LLMSpec(model='gpt-5.4-mini', reasoning_effort='medium', compact_threshold=50_000)
    fast = spec.fast()
    assert fast.service_tier == 'priority'
    # original untouched (frozen) and a distinct instance
    assert spec.service_tier is None
    assert fast is not spec
    # other fields preserved
    assert fast.model == 'gpt-5.4-mini'
    assert fast.reasoning_effort == 'medium'
    assert fast.compact_threshold == 50_000

  @pytest.mark.parametrize('level', ['low', 'medium', 'high', 'xhigh'])
  def test_with_effort_maps_shared_levels_through(self, level: str):
    assert LLMSpec().with_effort(level).reasoning_effort == level

  def test_with_effort_caps_max_at_the_provider_top(self):
    assert LLMSpec().with_effort('max').reasoning_effort == 'xhigh'

  def test_with_effort_rejects_a_level_outside_the_neutral_vocabulary(self):
    # 'minimal' is a valid reasoning_effort but not a neutral level — with_effort
    # speaks only the neutral vocabulary
    with pytest.raises(ValueError, match='unknown effort level'):
      LLMSpec().with_effort('minimal')

  def test_with_effort_returns_new_spec_preserving_other_knobs(self):
    spec = LLMSpec(model='gpt-5.4-mini', service_tier='priority')
    with_effort = spec.with_effort('high')
    assert with_effort.reasoning_effort == 'high'
    # original untouched (frozen) and a distinct instance
    assert spec.reasoning_effort is None
    assert with_effort is not spec
    # other fields preserved — composes with fast()'s service_tier
    assert with_effort.model == 'gpt-5.4-mini'
    assert with_effort.service_tier == 'priority'

  def test_frozen_rejects_mutation(self):
    spec = LLMSpec()
    # frozen dataclass raises FrozenInstanceError, a subclass of AttributeError
    with pytest.raises(AttributeError):
      spec.service_tier = 'priority'  # type: ignore[misc]

  def test_dump_round_trips_through_base_from_dict(self):
    spec = LLMSpec(
      model='gpt-5.4-mini',
      reasoning_effort='medium',
      service_tier='priority',
      compact_threshold=50_000,
    )
    restored = llm.llm.LLMSpec.from_dict(spec.dump())
    # frozen dataclass auto-generates __eq__ — single assertion covers every field
    assert restored == spec

  def test_dump_round_trip_handles_missing_optional_keys(self):
    # legacy / hand-written payloads may omit fields that were absent on write
    restored = llm.llm.LLMSpec.from_dict({'type': 'chat_gpt', 'model': 'gpt-5'})
    assert isinstance(restored, LLMSpec)
    assert restored == LLMSpec(model='gpt-5')

  def test_from_dict_works_without_pre_importing_provider_module(self):
    # Run in a fresh interpreter so `llm.llms.chat_gpt` is genuinely absent at
    # call time — simulates a process (e.g. an ad-hoc decisions_log reader)
    # that imports only `llm.llm` and expects `from_dict` to still dispatch.
    # In-process monkeypatching would leave the dataclass class registered on
    # `LLMSpec.__subclasses__` even after sys.modules restoration.
    import subprocess
    import sys

    from base.project_root import PROJECT_ROOT

    script = (
      'import sys; '
      "assert 'llm.llms.chat_gpt' not in sys.modules; "
      'from llm.llm import LLMSpec; '
      "spec = LLMSpec.from_dict({'type': 'chat_gpt', 'model': 'gpt-5'}); "
      "assert spec.model == 'gpt-5'; "
      "assert spec.TYPE == 'chat_gpt'"
    )
    result = subprocess.run(
      [sys.executable, '-c', script], capture_output=True, text=True, cwd=PROJECT_ROOT
    )
    assert result.returncode == 0, f'stderr: {result.stderr}'

  def test_reasoning_effort_values_match_openai(self):
    # the module mirrors openai's ReasoningEffort values locally so spec
    # validation needs no openai import; catch the mirror drifting on SDK bumps
    from typing import get_args

    import openai.types.shared

    import llm.llms.chat_gpt

    openai_values = get_args(get_args(openai.types.shared.ReasoningEffort)[0])
    assert frozenset(get_args(llm.llms.chat_gpt.ReasoningEffort)) == frozenset(openai_values)

  def test_importing_module_does_not_import_openai(self):
    # every bro module constructs an LLMSpec at class-definition time, so the
    # spec side must stay decoupled from the heavyweight openai package. Fresh
    # interpreter: in-process, other tests would already have openai loaded.
    import subprocess
    import sys

    from base.project_root import PROJECT_ROOT

    script = (
      'import sys; '
      'import llm.llms.chat_gpt; '
      "llm.llms.chat_gpt.LLMSpec(reasoning_effort='medium'); "
      "assert 'openai' not in sys.modules"
    )
    result = subprocess.run(
      [sys.executable, '-c', script], capture_output=True, text=True, cwd=PROJECT_ROOT
    )
    assert result.returncode == 0, f'stderr: {result.stderr}'


class TestUsageAccounting:
  @pytest.mark.asyncio
  async def test_cumulative_usage_maps_openai_classes(self):
    # cached input lands in cache_read, the uncached remainder in input,
    # cache_write stays 0, output keeps reasoning inside.
    gpt, _, captured = _make_chat_gpt_with_tracker()
    response = _fake_response(
      output=[_message_item('ok')],
      usage=_fake_usage(input_tokens=100, output_tokens=22, cached_tokens=30),
    )
    _install_responses(gpt, [response], captured)

    await gpt.send([{'role': 'user', 'content': 'hi'}])

    assert gpt.cumulative_usage() == {
      'gpt-5': {'input': 70, 'cache_write': 0, 'cache_read': 30, 'output': 22}
    }

  @pytest.mark.asyncio
  async def test_cumulative_usage_sums_across_calls_keyed_by_response_model(self):
    gpt, _, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')])
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

    await gpt.send([{'role': 'user', 'content': 'go'}])

    assert gpt.cumulative_usage() == {
      'gpt-5-2025-08-07': {'input': 25, 'cache_write': 0, 'cache_read': 25, 'output': 11}
    }

  @pytest.mark.asyncio
  async def test_agent_publishes_usage_file_after_every_call(self, tmp_path, monkeypatch):
    pointer = tmp_path / 'usage.json'
    monkeypatch.setenv(usage.USAGE_FILE_VARIABLE, str(pointer))
    gpt, _, captured = _make_chat_gpt_with_tracker([_StaticTool('ping')], agent='bro//ppp-dev')
    first = _fake_response(
      output=[_function_call_item('ping', call_id='c1')],
      usage=_fake_usage(input_tokens=10, output_tokens=5, cached_tokens=0),
    )
    second = _fake_response(
      output=[_message_item('done')],
      usage=_fake_usage(input_tokens=20, output_tokens=7, cached_tokens=4),
    )
    _install_responses(gpt, [first, second], captured)

    await gpt.send([{'role': 'user', 'content': 'go'}])

    published = usage.read_usage_file(pointer)
    assert published.agent == 'bro//ppp-dev'
    assert published.per_model == {
      'gpt-5': {'input': 26, 'cache_write': 0, 'cache_read': 4, 'output': 12}
    }

  @pytest.mark.asyncio
  async def test_no_agent_publishes_nothing(self, monkeypatch):
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    gpt, _, captured = _make_chat_gpt_with_tracker()
    _install_responses(gpt, [_fake_response(output=[_message_item('ok')])], captured)

    await gpt.send([{'role': 'user', 'content': 'hi'}])

    # publishing is gated on the agent identity; without it no pointer is minted
    assert usage.USAGE_FILE_VARIABLE not in os.environ
