from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from bro.llm.observer import (
  InterimAssistantTextEvent,
  ReasoningEvent,
  ToolCallEvent,
  ToolResultEvent,
  TurnCompletedEvent,
  TurnStartedEvent,
)
from bro.trails.client import TrailsClient
from bro.trails.display import (
  AssistantText,
  DisplayDataError,
  DisplaySession,
  HarnessEvent,
  InlineStepBody,
  InterimAssistantText,
  LaunchContextEntry,
  LiveDisplayObserver,
  LiveSource,
  NativeStep,
  Origin,
  RecordedAdapter,
  RecordedSource,
  RendererCapabilities,
  RetainedRenderer,
  SegmentBoundary,
  SpilledStepBody,
  TrailListRow,
  TrailMetadata,
  UserInput,
  preset,
)


class FakeClient:
  def __init__(self):
    self.headers: dict[str, dict[str, Any]] = {}
    self.messages: dict[str, list[dict[str, Any]]] = {}
    self.steps: dict[str, list[dict[str, Any]]] = {}
    self.contexts: dict[str, Any] = {}
    self.children: dict[str, list[dict[str, Any]]] = {}

  def get_trail(self, trail_id: str) -> dict[str, Any]:
    return self.headers[trail_id]

  def iter_messages(self, trail_id: str, *, after: int | None = None):
    yield from (
      message
      for message in self.messages.get(trail_id, [])
      if after is None or message['source']['step_id'] > after
    )

  def iter_steps(self, trail_id: str, *, after: int | None = None):
    yield from (
      step for step in self.steps.get(trail_id, []) if after is None or step['step_id'] > after
    )

  def get_launch_context(self, trail_id: str) -> Any:
    return self.contexts.get(trail_id)

  def iter_trails(self, **filters: Any):
    parent_id = filters.get('forked_from')
    if not isinstance(parent_id, str):
      return
    yield from self.children.get(parent_id, [])


def _client(fake: FakeClient) -> TrailsClient:
  return cast(TrailsClient, fake)


def _header(trail_id: str, **changes: Any) -> dict[str, Any]:
  return {
    'id': trail_id,
    'harness': 'bro',
    'bro': 'dev',
    'version': '1',
    'native': {'llm': {'model': 'gpt-5'}},
    'started_at': '2026-08-15T01:02:03Z',
    'end': None,
    'interactive': False,
    'surface': 'ask',
    'turn_count': 1,
    'usage': {},
    'models': ['gpt-5'],
    **changes,
  }


def _message(
  message_type: str, step_id: int, index: int = 0, timestamp: str | None = None, **fields: Any
) -> dict[str, Any]:
  return {
    'type': message_type,
    'ts': timestamp,
    'source': {'step_id': step_id, 'index': index},
    **fields,
  }


class CaptureRenderer:
  capabilities = RendererCapabilities(True, True, False, False)

  def __init__(self):
    self.operations = []

  def start(self, configuration):
    del configuration

  def apply(self, operation):
    self.operations.append(operation)

  def close(self):
    pass


class CaptureSession:
  def __init__(self):
    self.records = []

  def consume(self, record):
    self.records.append(record)


class TestMessages:
  def test_maps_every_generalized_message_type_without_inventing_timestamps(self):
    adapter = RecordedAdapter(_client(FakeClient()))
    records = adapter.message_records(
      'trail',
      [
        _message('system_prompt', 0, content='system'),
        _message(
          'user_input',
          1,
          content=[{'type': 'text', 'text': 'hello'}, {'type': 'image', 'url': 'x'}],
          isMeta=True,
          isSidechain=True,
        ),
        _message('llm_call', 2, model='gpt-5', usage={'input': 1}),
        _message('reasoning', 2, 1, content='think'),
        _message('assistant', 2, 2, content='working', terminal=False),
        _message(
          'tool_call', 2, 3, call_id='call', tool_name='repo__read', arguments={'path': 'x'}
        ),
        _message('tool_result', 3, call_id='call', content={'ok': True}, is_error=True),
        _message('assistant', 4, content='done', terminal=True),
        _message('error', 5, content='failed'),
        _message('harness_event', 6, raw={'type': 'progress', 'value': 2}),
      ],
    )

    assert [type(record).__name__ for record in records] == [
      'SystemPrompt',
      'UserInput',
      'LLMCall',
      'Reasoning',
      'InterimAssistantText',
      'ToolCall',
      'ToolResult',
      'AssistantText',
      'Error',
      'HarnessEvent',
    ]
    assert all(record.timestamp is None for record in records)
    user = records[1]
    assert isinstance(user, UserInput)
    assert user.content == 'hello'
    assert user.is_meta and user.is_sidechain
    harness_event = records[-1]
    assert isinstance(harness_event, HarnessEvent)
    assert harness_event.event == 'progress'

  @pytest.mark.parametrize(
    'message,match',
    [
      (_message('new_contract_type', 3), 'unknown message type'),
      (_message('tool_call', 3, call_id='', tool_name='read', arguments={}), 'call_id'),
      (_message('assistant', 3, content=1), 'assistant content'),
    ],
  )
  def test_contract_drift_and_malformed_known_records_fail_with_provenance(
    self, message: dict[str, Any], match: str
  ):
    adapter = RecordedAdapter(_client(FakeClient()))
    with pytest.raises(DisplayDataError, match=match) as caught:
      adapter.message_records('trail', [message])
    assert caught.value.source == RecordedSource('trail', 3)

  def test_paired_live_and_recorded_fixtures_normalize_and_present_equally(self):
    live_session = CaptureSession()
    observer = LiveDisplayObserver(
      cast(Any, live_session),
      run_id='run',
      now=lambda: datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC),
    )
    for event in (
      TurnStartedEvent('hello'),
      ReasoningEvent('think'),
      InterimAssistantTextEvent('working'),
      ToolCallEvent('call', 'repo__read', {'path': 'x'}),
      ToolResultEvent('call', 'repo__read', {'ok': True}),
      TurnCompletedEvent('done'),
    ):
      observer.on_event(event)

    recorded = RecordedAdapter(_client(FakeClient())).message_records(
      'trail',
      [
        _message('user_input', 0, content='hello'),
        _message('reasoning', 1, content='think'),
        _message('assistant', 2, content='working', terminal=False),
        _message('tool_call', 3, call_id='call', tool_name='repo__read', arguments={'path': 'x'}),
        _message('tool_result', 4, call_id='call', tool_name='repo__read', content={'ok': True}),
        _message('assistant', 5, content='done', terminal=True),
      ],
    )

    def normalize(records):
      return [
        replace(
          record,
          key=f'record:{sequence}',
          origin=Origin.LIVE,
          source=LiveSource('normalized', sequence),
          timestamp='2026-08-15T01:02:03+00:00',
        )
        for sequence, record in enumerate(records)
      ]

    normalized_live = normalize(live_session.records)
    normalized_recorded = normalize(recorded)
    assert normalized_recorded == normalized_live

    live_renderer = CaptureRenderer()
    with DisplaySession(preset('rewind-show'), live_renderer) as session:
      session.consume(normalized_live)
    recorded_renderer = CaptureRenderer()
    with DisplaySession(preset('rewind-show'), recorded_renderer) as session:
      session.consume(normalized_recorded)
    assert recorded_renderer.operations == live_renderer.operations


class TestCollection:
  def test_fork_chain_honors_the_exact_step_and_index_bound(self):
    fake = FakeClient()
    fake.headers['parent'] = _header('parent')
    fake.headers['child'] = _header(
      'child', forked_from={'trail_id': 'parent', 'step_id': 1, 'index': 1}
    )
    fake.messages['parent'] = [
      _message('user_input', 0, content='kept zero'),
      _message('assistant', 1, 0, content='kept one'),
      _message('assistant', 1, 1, content='kept two'),
      _message('assistant', 1, 2, content='cut by index'),
      _message('assistant', 2, content='cut by step'),
    ]
    fake.messages['child'] = [_message('user_input', 0, content='child')]

    records, after = RecordedAdapter(_client(fake)).conversation_records(fake.headers['child'])

    message_content = [
      record.content
      for record in records
      if isinstance(record, (UserInput, AssistantText, InterimAssistantText))
    ]
    assert message_content == ['kept zero', 'kept one', 'kept two', 'child']
    assert any(isinstance(record, SegmentBoundary) for record in records)
    assert after == 0

  def test_tool_call_ids_are_scoped_across_collected_segments(self):
    fake = FakeClient()
    fake.headers['parent'] = _header('parent')
    fake.headers['child'] = _header('child', forked_from={'trail_id': 'parent', 'step_id': 1})
    fake.messages['parent'] = [
      _message('tool_call', 0, call_id='same', tool_name='read', arguments={}),
      _message('tool_result', 1, call_id='same', content='parent result'),
    ]
    fake.messages['child'] = [
      _message('tool_call', 0, call_id='same', tool_name='read', arguments={}),
      _message('tool_result', 1, call_id='same', content='child result'),
    ]
    adapter = RecordedAdapter(_client(fake))
    records, _ = adapter.conversation_records(fake.headers['child'])
    renderer = RetainedRenderer()
    with DisplaySession(preset('rewind-show'), renderer) as session:
      session.consume(records)

    document = renderer.document()
    assert 'parent result' in document
    assert 'child result' in document


class TestStructures:
  def test_header_context_native_step_list_and_lineage_have_typed_records(self):
    fake = FakeClient()
    fake.headers['root'] = _header(
      'root', native={'llm': {'model': 'gpt-5'}, 'step_counts_by_kind': {'end': 0, 'user': 1}}
    )
    fake.headers['child'] = _header('child', forked_from={'trail_id': 'root', 'step_id': 2})
    fake.contexts['root'] = [{'title': 'git state', 'fields': {'branch': 'feature'}}]
    fake.children['root'] = [fake.headers['child']]
    adapter = RecordedAdapter(_client(fake))

    metadata = adapter.trail_metadata(fake.headers['root'])
    context = adapter.launch_context_records('root', fake.contexts['root'])
    inline = adapter.native_step(
      'root', {'step_id': 0, 'kind': 'user_input', 'ts': None, 'body': 'hello'}
    )
    spilled = adapter.native_step(
      'root',
      {
        'step_id': 1,
        'kind': 'llm_call',
        'ts': None,
        'body': {'s3': 'key', 'url': 'https://example.test/body', 'size': 42},
      },
    )
    row = adapter.trail_list_row(fake.headers['root'])
    lineage = adapter.lineage_records('child')

    assert isinstance(metadata, TrailMetadata)
    assert ('step kinds', {'user': 1}) in metadata.fields
    assert len(context) == 1 and isinstance(context[0], LaunchContextEntry)
    assert isinstance(inline, NativeStep) and isinstance(inline.body, InlineStepBody)
    assert isinstance(spilled.body, SpilledStepBody)
    assert spilled.body.size == 42
    assert isinstance(row, TrailListRow)
    assert [node.trail_id for node in lineage] == ['root', 'child']
    assert lineage[1].highlighted

  def test_native_step_never_fetches_a_spilled_body(self):
    fake = FakeClient()
    adapter = RecordedAdapter(_client(fake))
    record = adapter.native_step(
      'trail',
      {
        'step_id': 4,
        'kind': 'tool_result',
        'body': {'s3': 'key', 'url': 'https://example.test/body', 'size': 100},
      },
    )
    assert isinstance(record.body, SpilledStepBody)
