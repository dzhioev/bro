import json
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import patch

import pytest
from openai.types.responses import Response

import llm.llms.chat_gpt as chat_gpt_module
from bro.fork import fork, latest_fork_point, replay_messages
from llm.tracker import NullTracker, Tracker
from trails.model import ForkedFrom, RecordedTrail, Step, Trail

_SYS_TEXT = 'you are a test bro'


def _trail_header(
  *,
  trail_id: str = 'trail-1',
  bro: str = 'bro',
  llm_spec: Optional[dict] = None,
) -> Trail:
  return Trail(
    id=trail_id,
    harness='bro',
    bro=bro,
    version='1',
    native={'llm': llm_spec if llm_spec is not None else {'type': 'chat_gpt', 'model': 'gpt-5'}},
    started_at='2026-06-07T00:00:00.000000Z',
    interactive=False,
    surface='ask',
    forked_from=None,
  )


def _step(
  kind: str,
  body: Any,
  *,
  step_id: int,
  trail_id: str = 'trail-1',
  **extras: Any,
) -> Step:
  return Step(
    trail_id=trail_id,
    step_id=step_id,
    ts='2026-06-07T00:00:00.000000Z',
    kind=cast(Any, kind),
    body=body,
    extras=extras,
  )


def _output_message(text: str, *, role: str = 'assistant') -> dict:
  return {'type': 'message', 'role': role, 'content': [{'type': 'output_text', 'text': text}]}


def _output_function_call(name: str, *, call_id: str, arguments: str = '{}') -> dict:
  return {'type': 'function_call', 'name': name, 'arguments': arguments, 'call_id': call_id}


def _llm_call_body(*output_items: dict, request_input: Optional[list] = None) -> dict:
  return {
    'request': {'model': 'gpt-5', 'input': request_input if request_input is not None else []},
    'response': {'id': 'resp', 'output': list(output_items)},
  }


class TestReplayMessages:
  def test_raises_when_system_prompt_step_missing(self):
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[_step('user_input', 'hello', step_id=1, turn_index=0)],
    )
    with pytest.raises(ValueError, match='no system_prompt step'):
      replay_messages(trail, 1)

  def test_raises_when_step_id_not_found(self):
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
      ],
    )
    with pytest.raises(ValueError, match='not found'):
      replay_messages(trail, 99)

  def test_returns_only_system_and_user_when_forked_at_first_user_input(self):
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(_output_message('hi back')),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
      ],
    )
    assert replay_messages(trail, 1) == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
    ]

  def test_includes_response_output_when_forked_at_terminal_llm_call(self):
    assistant_msg = _output_message('hi back')
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(assistant_msg),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
      ],
    )
    assert replay_messages(trail, 2) == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      assistant_msg,
    ]

  def test_strips_response_only_fields_from_output_items(self):
    # response.model_dump carries fields the API rejects when the items are
    # replayed as input: `status` on every item kind, null-valued optionals
    # (content / encrypted_content) on reasoning items.
    raw_reasoning = {
      'type': 'reasoning',
      'id': 'rs_1',
      'summary': [],
      'content': None,
      'encrypted_content': None,
      'status': None,
    }
    raw_message = {
      'type': 'message',
      'role': 'assistant',
      'id': 'msg_1',
      'status': 'completed',
      'content': [{'type': 'output_text', 'text': 'hi back'}],
    }
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(raw_reasoning, raw_message),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
      ],
    )
    assert replay_messages(trail, 2) == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      {'type': 'reasoning', 'id': 'rs_1', 'summary': []},
      {
        'type': 'message',
        'role': 'assistant',
        'id': 'msg_1',
        'content': [{'type': 'output_text', 'text': 'hi back'}],
      },
    ]

  def test_drops_compaction_items_from_output(self):
    # a run that crossed the compact_threshold records a `compaction` item in
    # that turn's response.output. replay carries the full verbatim history the
    # summary stands for, so the item must not be re-sent as input.
    compaction = {'type': 'compaction', 'id': 'cmp_1', 'encrypted_content': 'opaque'}
    assistant_msg = _output_message('hi back')
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(compaction, assistant_msg),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
      ],
    )
    assert replay_messages(trail, 2) == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      assistant_msg,
    ]

  def test_appends_function_call_output_for_tool_result(self):
    call_item = _output_function_call('add', call_id='c1')
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'add 2+2', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(call_item),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
        _step('tool_call', None, step_id=3, turn_index=1, tool_name='add', call_id='c1'),
        _step('tool_result', '4', step_id=4, turn_index=1, tool_name='add', call_id='c1'),
      ],
    )
    assert replay_messages(trail, 4) == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'add 2+2'},
      call_item,
      {'type': 'function_call_output', 'call_id': 'c1', 'output': '4'},
    ]

  def test_dict_tool_result_is_json_encoded(self):
    call_item = _output_function_call('fetch', call_id='c1')
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'fetch x', step_id=1, turn_index=0),
        _step('llm_call', _llm_call_body(call_item), step_id=2, turn_index=1, response_id='r'),
        _step('tool_call', None, step_id=3, turn_index=1, tool_name='fetch', call_id='c1'),
        _step(
          'tool_result',
          {'rows': [1, 2]},
          step_id=4,
          turn_index=1,
          tool_name='fetch',
          call_id='c1',
        ),
      ],
    )
    result = replay_messages(trail, 4)
    fco = result[-1]
    assert fco['output'] == json.dumps({'rows': [1, 2]})

  def test_multi_turn_after_second_user_input(self):
    first_reply = _output_message('first reply')
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step('llm_call', _llm_call_body(first_reply), step_id=2, turn_index=1, response_id='r1'),
        _step('user_input', 'follow up', step_id=3, turn_index=2),
      ],
    )
    assert replay_messages(trail, 3) == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      first_reply,
      {'role': 'user', 'content': 'follow up'},
    ]

  def test_skips_kinds_not_carried_in_input(self):
    # reasoning / assistant / tool_call / end are produced by ChatGPT as
    # side-channel steps; the canonical input shapes (assistant message,
    # function_call) live on the llm_call's response.output items. ensure
    # replay does not double-emit them.
    assistant_msg = _output_message('reply')
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hi', step_id=1, turn_index=0),
        _step('llm_call', _llm_call_body(assistant_msg), step_id=2, turn_index=1, response_id='r'),
        _step('reasoning', 'thinking', step_id=3, turn_index=1),
        _step('assistant', 'reply', step_id=4, turn_index=1, terminal=True),
        _step('end', {'reason': 'terminal'}, step_id=5, turn_index=1),
      ],
    )
    result = replay_messages(trail, 5)
    # only one assistant payload, taken from llm_call.response.output
    assert result.count(assistant_msg) == 1
    assert all(isinstance(item, dict) for item in result)


class TestReplayMessagesAcrossForkChain:
  def _forked_from(self) -> RecordedTrail:
    return _simple_trail()

  def _child(self) -> RecordedTrail:
    # a fork of trail-1 at its terminal llm_call, with one exchange of its own
    return RecordedTrail(
      header=Trail(
        id='trail-2',
        harness='bro',
        bro='bro',
        version='1',
        native={'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
        started_at='2026-06-08T00:00:00.000000Z',
        interactive=True,
        surface='call',
        forked_from=ForkedFrom(
          trail_id='trail-1',
          step_id=2,
        ),
      ),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, trail_id='trail-2', turn_index=0),
        _step('user_input', 'continue', step_id=1, trail_id='trail-2', turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(_output_message('continued')),
          step_id=2,
          trail_id='trail-2',
          turn_index=1,
          response_id='r2',
        ),
      ],
    )

  def test_prepends_ancestor_prefix_via_fetch_forked_from(self):
    forked_froms = {'trail-1': self._forked_from()}
    result = replay_messages(self._child(), 2, fetch_forked_from=lambda tid: forked_froms[tid])
    assert result == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      _output_message('hi back'),
      {'role': 'user', 'content': 'continue'},
      _output_message('continued'),
    ]

  def test_raises_on_fork_trail_without_fetch_forked_from(self):
    with pytest.raises(ValueError, match='fetch_forked_from'):
      replay_messages(self._child(), 2)

  def test_fork_at_system_prompt_step_replays_only_the_ancestor_prefix(self):
    # an empty continuation (a fork with no exchanges of its own) resumes as
    # exactly the ancestor conversation
    forked_froms = {'trail-1': self._forked_from()}
    child = self._child()
    empty_child = RecordedTrail(header=child.header, steps=child.steps[:1])
    result = replay_messages(empty_child, 0, fetch_forked_from=lambda tid: forked_froms[tid])
    assert result == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      _output_message('hi back'),
    ]


class TestLatestForkPoint:
  def test_picks_the_terminal_llm_call(self):
    trail = _simple_trail()
    assert latest_fork_point(trail) == 2

  def test_picks_a_trailing_user_input(self):
    # killed after the user sent a message but before the model replied
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(_output_message('hi back')),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
        _step('user_input', 'follow up', step_id=3, turn_index=2),
      ],
    )
    assert latest_fork_point(trail) == 3

  def test_skips_an_llm_call_with_unanswered_function_calls(self):
    # killed mid-tool-loop: the last llm_call's function_call has no
    # tool_result, so the resume point falls back to the last consistent step
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'go', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(_output_function_call('lookup', call_id='call-1')),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
        _step('tool_call', None, step_id=3, turn_index=1, tool_name='lookup', call_id='call-1'),
      ],
    )
    assert latest_fork_point(trail) == 1

  def test_picks_the_tool_result_that_completes_the_turn(self):
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'go', step_id=1, turn_index=0),
        _step(
          'llm_call',
          _llm_call_body(
            _output_function_call('lookup', call_id='call-1'),
            _output_function_call('lookup', call_id='call-2'),
          ),
          step_id=2,
          turn_index=1,
          response_id='r1',
        ),
        _step('tool_result', 'a', step_id=3, turn_index=1, tool_name='lookup', call_id='call-1'),
        _step('tool_result', 'b', step_id=4, turn_index=1, tool_name='lookup', call_id='call-2'),
      ],
    )
    assert latest_fork_point(trail) == 4

  def test_raises_on_a_trail_with_nothing_to_resume(self):
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[_step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0)],
    )
    with pytest.raises(ValueError, match='no step to resume from'):
      latest_fork_point(trail)

  def test_fork_trail_falls_back_to_its_system_prompt_step(self):
    # an empty continuation still resumes — through the ancestor prefix its
    # forked_from pointer carries
    trail = RecordedTrail(
      header=_trail_header(),
      steps=[_step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0)],
    )
    forked = RecordedTrail(
      header=Trail(
        id='trail-2',
        harness='bro',
        bro='bro',
        version='1',
        native={'llm': {'type': 'chat_gpt', 'model': 'gpt-5'}},
        started_at='2026-06-08T00:00:00.000000Z',
        interactive=True,
        surface='call',
        forked_from=ForkedFrom(
          trail_id='trail-1',
          step_id=2,
        ),
      ),
      steps=trail.steps,
    )
    assert latest_fork_point(forked) == 0


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
    return 'forked-trail-id'

  def step(self, kind, body, **extras) -> None:
    self.steps.append((kind, body, extras))

  def end_trail(self, reason, detail=None) -> None:
    self.ended.append(reason)


def _fake_usage(*, input_tokens=10, output_tokens=20, reasoning_tokens=0):
  return SimpleNamespace(
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
  )


def _fake_response(*, output: list, response_id: str = 'resp_fork', dump_payload=None) -> Response:
  namespace = SimpleNamespace(
    id=response_id,
    output=output,
    usage=_fake_usage(),
    model='gpt-5',
  )
  payload = dump_payload if dump_payload is not None else {'id': response_id, 'output': []}
  namespace.model_dump = lambda mode='json': payload
  return cast(Response, namespace)


def _message_item(text: str):
  return SimpleNamespace(
    type='message',
    content=[SimpleNamespace(type='output_text', text=text)],
  )


def _install_responses(gpt: chat_gpt_module.ChatGPT, sequence: list, captured: list[dict]) -> None:
  iterator = iter(sequence)

  def create(**kwargs):
    captured.append(kwargs)
    return next(iterator)

  gpt.client = cast(Any, SimpleNamespace(responses=SimpleNamespace(create=create)))


def _patch_chat_gpt_create_llm(stub_responses):
  """patch chat_gpt.LLMSpec.create_llm so fork() builds a controllable ChatGPT
  with a stub responses.create. returns (patcher_context, captured_kwargs, created_gpts).
  """
  captured_kwargs: list[dict] = []
  created: list[chat_gpt_module.ChatGPT] = []

  def _create(self, mcp_servers=None, observer=None, tracker=None, agent=None):
    gpt = chat_gpt_module.ChatGPT(
      api_key='dummy',
      model=self.model,
      reasoning_effort=self.reasoning_effort,
      service_tier=self.service_tier,
      observer=observer,
      tracker=tracker,
    )
    gpt._openai_tools = []
    _install_responses(gpt, stub_responses, captured_kwargs)
    created.append(gpt)
    return gpt

  context = patch.object(chat_gpt_module.LLMSpec, 'create_llm', _create)
  return context, captured_kwargs, created


def _simple_trail(**header_overrides: Any) -> RecordedTrail:
  reply = _output_message('hi back')
  return RecordedTrail(
    header=_trail_header(**header_overrides),
    steps=[
      _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
      _step('user_input', 'hello', step_id=1, turn_index=0),
      _step('llm_call', _llm_call_body(reply), step_id=2, turn_index=1, response_id='r1'),
    ],
  )


class TestForkLinkage:
  def test_start_trail_records_forked_from_pointer(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, _ = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      fork(forked_from_trail, 2, tracker=tracker, surface='test')
    assert len(tracker.headers) == 1
    header = tracker.headers[0]
    assert header['forked_from'] == ForkedFrom(
      trail_id='trail-1',
      step_id=2,
    )
    assert header['surface'] == 'test'
    assert header['interactive'] is True
    assert header['bro'] == 'bro'

  def test_surface_override_labels_the_new_trail(self):
    # `call --resume` labels its continuations 'call' so they are themselves
    # picked up by the next resume-latest lookup
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, _ = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      bro = fork(forked_from_trail, 2, tracker=tracker, surface='call')
    assert tracker.headers[0]['surface'] == 'call'
    assert bro.trail_id == 'forked-trail-id'

  def test_records_resolved_system_prompt(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, _ = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      fork(forked_from_trail, 2, tracker=tracker, surface='test')
    assert tracker.headers[0]['system_prompt'] == _SYS_TEXT

  def test_system_prompt_override_replaces_prefix_and_header(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, created = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      bro = fork(
        forked_from_trail, 2, system_prompt='swapped prompt', tracker=tracker, surface='test'
      )
    assert tracker.headers[0]['system_prompt'] == 'swapped prompt'
    assert bro.system_prompt == 'swapped prompt'
    # prefix on the new ChatGPT's seam carries the override at index 0
    seeded = created[0]._input_prefix
    assert seeded is not None
    assert seeded[0] == {'role': 'system', 'content': 'swapped prompt'}


class TestForkRecording:
  def test_record_false_uses_null_tracker(self):
    forked_from_trail = _simple_trail()
    context, _, _ = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      bro = fork(forked_from_trail, 2, record=False, surface='test')
    assert isinstance(bro._tracker, NullTracker)

  def test_record_true_uses_explicit_tracker(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, _ = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      bro = fork(forked_from_trail, 2, tracker=tracker, surface='test')
    assert bro._tracker is tracker


class TestForkSpec:
  def test_defaults_to_forked_from_spec(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, created = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      fork(forked_from_trail, 2, tracker=tracker, surface='test')
    assert created[0].model == 'gpt-5'
    assert tracker.headers[0]['llm_spec'] == {
      'type': 'chat_gpt',
      'model': 'gpt-5',
      'reasoning_effort': None,
      'service_tier': None,
      'compact_threshold': None,
    }

  def test_cross_model_spec_override(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    override = chat_gpt_module.LLMSpec(model='gpt-5.4-mini', reasoning_effort='medium')
    context, _, created = _patch_chat_gpt_create_llm([_fake_response(output=[_message_item('ok')])])
    with context:
      fork(forked_from_trail, 2, llm_spec=override, tracker=tracker, surface='test')
    assert created[0].model == 'gpt-5.4-mini'
    assert created[0]._reasoning_effort == 'medium'
    assert tracker.headers[0]['llm_spec']['model'] == 'gpt-5.4-mini'
    assert tracker.headers[0]['llm_spec']['reasoning_effort'] == 'medium'


class TestForkServerSidePath:
  """default path when forking right after an `llm_call` with the same spec:
  the new LLM is seeded with `previous_response_id` and OpenAI carries the
  whole prefix server-side. only the new user message hits the wire.
  """

  @pytest.mark.asyncio
  async def test_send_seeds_previous_response_id_and_omits_prefix(self):
    forked_from_trail = _simple_trail()
    context, captured, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('continuation')])]
    )
    with context:
      bro = fork(forked_from_trail, 2, record=False, surface='test')
      result = await bro.send('follow up', surface='test')
    assert result == 'continuation'
    assert len(captured) == 1
    assert captured[0].get('previous_response_id') == 'r1'
    assert captured[0]['input'] == [{'role': 'user', 'content': 'follow up'}]

  @pytest.mark.asyncio
  async def test_user_input_step_recorded_only_for_new_message(self):
    forked_from_trail = _simple_trail()
    tracker = _RecordingTracker()
    context, _, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('continuation')])]
    )
    with context:
      bro = fork(forked_from_trail, 2, tracker=tracker, surface='test')
      await bro.send('follow up', surface='test')
    user_inputs = [s for s in tracker.steps if s[0] == 'user_input']
    # the replayed user input does NOT get re-emitted on the new trail — only
    # the new message the caller passed to .send()
    assert len(user_inputs) == 1
    assert user_inputs[0][1] == 'follow up'

  @pytest.mark.asyncio
  async def test_multi_send_chains_response_ids(self):
    forked_from_trail = _simple_trail()
    context, captured, _ = _patch_chat_gpt_create_llm(
      [
        _fake_response(output=[_message_item('first')], response_id='r2'),
        _fake_response(output=[_message_item('second')], response_id='r3'),
      ]
    )
    with context:
      bro = fork(forked_from_trail, 2, record=False, surface='test')
      await bro.send('msg one', surface='test')
      await bro.send('msg two', surface='test')
    # first send carries the seeded fork-point response_id; the second chains
    # off the first response's id. interleaved calls only ship the new user
    # message — the prefix lives server-side throughout.
    assert captured[0].get('previous_response_id') == 'r1'
    assert captured[0]['input'] == [{'role': 'user', 'content': 'msg one'}]
    assert captured[1].get('previous_response_id') == 'r2'
    assert captured[1]['input'] == [{'role': 'user', 'content': 'msg two'}]


class TestForkClientSideReplay:
  """client-side replay covers everything server-side can't:
  - fork at a non-`llm_call` step (e.g. a user_input)
  - cross-model / cross-provider forks
  - swapped system prompt
  """

  @pytest.mark.asyncio
  async def test_fork_at_first_user_input(self):
    # the fork point isn't an `llm_call`; server-side has nothing to anchor on,
    # so the replay path is the only option.
    forked_from_trail = _simple_trail()
    context, captured, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('rerun')])]
    )
    with context:
      bro = fork(forked_from_trail, 1, record=False, surface='test')
      assert await bro.send('rerun please', surface='test') == 'rerun'
    api_input = captured[0]['input']
    # forking right after the first user_input replays system + that input, then
    # the new user message lands at the end (re-ask path)
    assert api_input == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      {'role': 'user', 'content': 'rerun please'},
    ]

  @pytest.mark.asyncio
  async def test_fork_after_later_user_input(self):
    first_reply = _output_message('first reply')
    forked_from_trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'hello', step_id=1, turn_index=0),
        _step('llm_call', _llm_call_body(first_reply), step_id=2, turn_index=1, response_id='r1'),
        _step('user_input', 'follow up', step_id=3, turn_index=2),
        _step(
          'llm_call',
          _llm_call_body(_output_message('second reply')),
          step_id=4,
          turn_index=3,
          response_id='r2',
        ),
      ],
    )
    context, captured, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('forked answer')])]
    )
    with context:
      bro = fork(forked_from_trail, 3, record=False, surface='test')
      await bro.send('actually try this', surface='test')
    api_input = captured[0]['input']
    assert api_input == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'hello'},
      first_reply,
      {'role': 'user', 'content': 'follow up'},
      {'role': 'user', 'content': 'actually try this'},
    ]

  @pytest.mark.asyncio
  async def test_fork_after_tool_loop_includes_function_call_outputs(self):
    call_item = _output_function_call('lookup', call_id='c1')
    assistant_reply = _output_message('done')
    forked_from_trail = RecordedTrail(
      header=_trail_header(),
      steps=[
        _step('system_prompt', _SYS_TEXT, step_id=0, turn_index=0),
        _step('user_input', 'go', step_id=1, turn_index=0),
        _step('llm_call', _llm_call_body(call_item), step_id=2, turn_index=1, response_id='r1'),
        _step('tool_call', None, step_id=3, turn_index=1, tool_name='lookup', call_id='c1'),
        _step(
          'tool_result',
          'tool answer',
          step_id=4,
          turn_index=1,
          tool_name='lookup',
          call_id='c1',
        ),
        _step(
          'llm_call',
          _llm_call_body(assistant_reply),
          step_id=5,
          turn_index=2,
          response_id='r2',
        ),
      ],
    )
    context, captured, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('continued')])]
    )
    with context:
      # forcing client-side via a no-op system_prompt override — the path
      # picker treats any override as "client-side only" since the cached
      # server-side prefix can't be restated.
      bro = fork(forked_from_trail, 5, system_prompt=_SYS_TEXT, record=False, surface='test')
      await bro.send('next', surface='test')
    api_input = captured[0]['input']
    # prefix preserves: system, user, function_call, function_call_output (with
    # matching call_id), final assistant message — then the new user message
    assert api_input == [
      {'role': 'system', 'content': _SYS_TEXT},
      {'role': 'user', 'content': 'go'},
      call_item,
      {'type': 'function_call_output', 'call_id': 'c1', 'output': 'tool answer'},
      assistant_reply,
      {'role': 'user', 'content': 'next'},
    ]

  @pytest.mark.asyncio
  async def test_cross_model_falls_back_to_client_side(self):
    # different model on the new spec disqualifies server-side (a
    # response_id is pinned to the originating model on OpenAI's side).
    forked_from_trail = _simple_trail()
    override = chat_gpt_module.LLMSpec(model='gpt-5.4-mini')
    context, captured, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('ok')])]
    )
    with context:
      bro = fork(forked_from_trail, 2, llm_spec=override, record=False, surface='test')
      await bro.send('next', surface='test')
    assert captured[0].get('previous_response_id') is None
    api_input = captured[0]['input']
    assert api_input[0] == {'role': 'system', 'content': _SYS_TEXT}
    assert api_input[-1] == {'role': 'user', 'content': 'next'}

  @pytest.mark.asyncio
  async def test_system_prompt_override_forces_client_side(self):
    # the cached server-side prefix already carries the prompt; we can't
    # restate it through `previous_response_id`, so any override forces a
    # full replay so the swapped prompt actually takes effect.
    forked_from_trail = _simple_trail()
    context, captured, _ = _patch_chat_gpt_create_llm(
      [_fake_response(output=[_message_item('ok')])]
    )
    with context:
      bro = fork(forked_from_trail, 2, system_prompt='swapped prompt', record=False, surface='test')
      await bro.send('next', surface='test')
    assert captured[0].get('previous_response_id') is None
    assert captured[0]['input'][0] == {'role': 'system', 'content': 'swapped prompt'}

  @pytest.mark.asyncio
  async def test_subsequent_send_does_not_re_inject_prefix(self):
    # `_input_prefix` is consumed exactly once on first send. forced
    # client-side here via prompt override so the prefix path actually fires.
    forked_from_trail = _simple_trail()
    context, captured, _ = _patch_chat_gpt_create_llm(
      [
        _fake_response(output=[_message_item('first')], response_id='resp_a'),
        _fake_response(output=[_message_item('second')], response_id='resp_b'),
      ]
    )
    with context:
      bro = fork(forked_from_trail, 2, system_prompt=_SYS_TEXT, record=False, surface='test')
      await bro.send('msg one', surface='test')
      await bro.send('msg two', surface='test')
    # second call goes through `previous_response_id` and only ships the new
    # user message — the replayed prefix never re-injects
    assert captured[1].get('previous_response_id') == 'resp_a'
    assert captured[1]['input'] == [{'role': 'user', 'content': 'msg two'}]
