import threading
from collections.abc import Iterable

import pytest

from bro.trails.display import (
  Append,
  AssistantText,
  BlockKind,
  BlockOperation,
  ColorMode,
  ContentLimits,
  DisplayConfig,
  DisplayDataError,
  DisplaySession,
  InlineStepBody,
  Layout,
  LineageNode,
  LiveSource,
  NativeStep,
  Origin,
  OutputRoute,
  OutputRoutes,
  Reasoning,
  RecordedSource,
  RecordFilter,
  RecordKind,
  Remove,
  RendererCapabilities,
  RetainedRenderer,
  TimestampPolicy,
  ToolCall,
  ToolResult,
  TrailListRow,
  TrailMetadata,
  TransientActivity,
  Update,
  Verbosity,
  preset,
)


class CaptureRenderer:
  capabilities = RendererCapabilities(True, True, False, False)

  def __init__(self):
    self.configuration: DisplayConfig | None = None
    self.operations: list[BlockOperation] = []
    self.closed = False

  def start(self, configuration: DisplayConfig) -> None:
    assert self.configuration is None
    self.configuration = configuration

  def apply(self, operation: BlockOperation) -> None:
    self.operations.append(operation)

  def close(self) -> None:
    self.closed = True


def _live_call(sequence: int = 1, *, key: str = 'call', call_id: str = 'c1') -> ToolCall:
  return ToolCall(
    key=key,
    origin=Origin.LIVE,
    source=LiveSource('run', sequence),
    call_id=call_id,
    tool_name='repo__search',
    arguments={'query': 'trails'},
  )


def _live_result(sequence: int = 2, *, key: str = 'result', call_id: str = 'c1') -> ToolResult:
  return ToolResult(
    key=key,
    origin=Origin.LIVE,
    source=LiveSource('run', sequence),
    call_id=call_id,
    tool_name='repo__search',
    result={'hits': 2},
  )


def _consume(
  records: Iterable[ToolCall | ToolResult], configuration: DisplayConfig | None = None
) -> CaptureRenderer:
  renderer = CaptureRenderer()
  with DisplaySession(configuration or DisplayConfig(), renderer) as session:
    session.consume(records)
  return renderer


class TestOrdering:
  def test_rejects_nonmonotonic_input_within_a_segment(self):
    renderer = CaptureRenderer()
    session = DisplaySession(DisplayConfig(), renderer)
    session.consume(
      AssistantText(
        key='later',
        origin=Origin.RECORDED,
        source=RecordedSource('trail', 2),
        content='later',
      )
    )
    with pytest.raises(DisplayDataError, match='non-monotonic'):
      session.consume(
        AssistantText(
          key='earlier',
          origin=Origin.RECORDED,
          source=RecordedSource('trail', 1),
          content='earlier',
        )
      )
    session.close()

  def test_tracks_each_recorded_segment_independently(self):
    renderer = CaptureRenderer()
    with DisplaySession(DisplayConfig(), renderer) as session:
      session.consume(
        [
          AssistantText(
            key='a',
            origin=Origin.RECORDED,
            source=RecordedSource('a', 9),
            content='a',
          ),
          AssistantText(
            key='b',
            origin=Origin.RECORDED,
            source=RecordedSource('b', 1),
            content='b',
          ),
        ]
      )
    assert len(renderer.operations) == 2

  def test_rejects_duplicate_stable_record_keys(self):
    record = AssistantText(
      key='same',
      origin=Origin.LIVE,
      source=LiveSource('run', 1),
      content='one',
    )
    renderer = CaptureRenderer()
    session = DisplaySession(DisplayConfig(), renderer)
    session.consume(record)
    with pytest.raises(DisplayDataError, match='duplicate display record key'):
      session.consume(record)
    session.close()


class TestCorrelation:
  def test_call_then_result_updates_one_stable_block(self):
    renderer = _consume([_live_call(), _live_result()])
    append = renderer.operations[0]
    update = renderer.operations[1]
    assert isinstance(append, Append)
    assert isinstance(update, Update)
    assert append.block.id == update.block.id
    assert append.block.pending
    assert not update.block.pending
    assert append.block.label == 'repo::search'
    assert [item.label for item in update.block.items] == ['arguments', 'result']

  def test_result_before_call_is_buffered_until_the_call_arrives(self):
    renderer = _consume([_live_result(sequence=1), _live_call(sequence=2)])
    assert len(renderer.operations) == 1
    operation = renderer.operations[0]
    assert isinstance(operation, Append)
    assert not operation.block.pending
    assert [item.label for item in operation.block.items] == ['arguments', 'result']

  def test_hidden_result_settles_pending_state_without_showing_body(self):
    renderer = _consume([_live_call(), _live_result()], preset('call'))
    assert len(renderer.operations) == 2
    update = renderer.operations[1]
    assert isinstance(update, Update)
    assert not update.block.pending
    assert [item.label for item in update.block.items] == ['arguments', 'result']
    assert update.block.items[-1].text == ''

  def test_hidden_call_with_visible_result_produces_a_standalone_result(self):
    configuration = DisplayConfig(
      record_filter=RecordFilter.excluding(RecordKind.TOOL_CALL),
    )
    renderer = _consume([_live_call(), _live_result()], configuration)
    assert len(renderer.operations) == 1
    operation = renderer.operations[0]
    assert isinstance(operation, Append)
    assert operation.block.label == 'tool result · repo::search'
    assert operation.block.items[0].text.startswith('{')

  @pytest.mark.parametrize('duplicate', ['call', 'result'])
  def test_duplicate_tool_halves_fail_fast(self, duplicate: str):
    renderer = CaptureRenderer()
    session = DisplaySession(DisplayConfig(), renderer)
    if duplicate == 'call':
      session.consume(_live_call())
      with pytest.raises(DisplayDataError, match='duplicate tool call'):
        session.consume(_live_call(sequence=2, key='call-2'))
    else:
      session.consume(_live_result(sequence=1))
      with pytest.raises(DisplayDataError, match='duplicate tool result'):
        session.consume(_live_result(sequence=2, key='result-2'))
    session.close()

  def test_equal_call_ids_in_different_trails_do_not_collide(self):
    records = [
      ToolCall(
        key='a-call',
        origin=Origin.RECORDED,
        source=RecordedSource('a', 1),
        call_id='same',
        tool_name='x',
        arguments={},
      ),
      ToolCall(
        key='b-call',
        origin=Origin.RECORDED,
        source=RecordedSource('b', 1),
        call_id='same',
        tool_name='x',
        arguments={},
      ),
      ToolResult(
        key='a-result',
        origin=Origin.RECORDED,
        source=RecordedSource('a', 2),
        call_id='same',
        result='a',
      ),
      ToolResult(
        key='b-result',
        origin=Origin.RECORDED,
        source=RecordedSource('b', 2),
        call_id='same',
        result='b',
      ),
    ]
    renderer = _consume(records)
    appended_ids = {
      operation.block.id for operation in renderer.operations if isinstance(operation, Append)
    }
    assert len(appended_ids) == 2

  def test_close_flushes_pending_calls_and_orphan_results(self):
    renderer = CaptureRenderer()
    session = DisplaySession(DisplayConfig(), renderer)
    session.consume(
      [
        _live_call(call_id='pending'),
        _live_result(sequence=2, key='orphan', call_id='orphan'),
      ]
    )
    session.close()
    assert isinstance(renderer.operations[-2], Update)
    assert renderer.operations[-2].block.items[-1].text == 'result unavailable'
    assert isinstance(renderer.operations[-1], Append)
    assert renderer.operations[-1].block.id.endswith(':orphan')
    assert renderer.operations[-1].block.label == 'tool result · repo::search'


class TestPresentation:
  def test_groups_same_step_content_and_reports_truncation(self):
    renderer = CaptureRenderer()
    configuration = DisplayConfig(
      verbosity=Verbosity.COMPACT,
      content_limits=ContentLimits(compact=4),
      layout=Layout.CONVERSATION,
    )
    with DisplaySession(configuration, renderer) as session:
      session.consume(
        [
          Reasoning(
            key='one',
            origin=Origin.RECORDED,
            source=RecordedSource('trail', 1, 1),
            content='abcdef',
          ),
          Reasoning(
            key='two',
            origin=Origin.RECORDED,
            source=RecordedSource('trail', 1, 2),
            content='ghijkl',
          ),
        ]
      )
    assert isinstance(renderer.operations[0], Append)
    assert isinstance(renderer.operations[1], Update)
    grouped = renderer.operations[1].block
    assert [item.text for item in grouped.items] == ['abcd', 'ghij']
    assert [item.omitted_characters for item in grouped.items] == [2, 2]

  def test_control_bytes_are_neutralized_in_content_and_labels(self):
    renderer = CaptureRenderer()
    with DisplaySession(DisplayConfig(), renderer) as session:
      session.consume(
        [
          AssistantText(
            key='message',
            origin=Origin.LIVE,
            source=LiveSource('run', 1),
            content='hello\x00\nworld\t!',
          ),
          ToolCall(
            key='unsafe',
            origin=Origin.LIVE,
            source=LiveSource('run', 2),
            call_id='call',
            tool_name='repo__bad\x1b',
            arguments={},
          ),
        ]
      )
    message = renderer.operations[0]
    tool = renderer.operations[1]
    assert isinstance(message, Append)
    assert isinstance(tool, Append)
    assert '\x00' not in message.block.items[0].text
    assert '\n' in message.block.items[0].text
    assert '\t' in message.block.items[0].text
    assert '\x1b' not in tool.block.label

  def test_routes_and_timestamp_policy_are_applied_to_blocks(self):
    renderer = CaptureRenderer()
    configuration = DisplayConfig(
      timestamps=TimestampPolicy.PLACEHOLDER,
      routes=OutputRoutes(reply=OutputRoute.CONVERSATION, trace=OutputRoute.METADATA),
    )
    with DisplaySession(configuration, renderer) as session:
      session.consume(
        [
          Reasoning(
            key='reasoning',
            origin=Origin.LIVE,
            source=LiveSource('run', 1),
            content='thinking',
          ),
          AssistantText(
            key='reply',
            origin=Origin.LIVE,
            source=LiveSource('run', 2),
            content='done',
          ),
        ]
      )
    reasoning = renderer.operations[0]
    reply = renderer.operations[1]
    assert isinstance(reasoning, Append)
    assert isinstance(reply, Append)
    assert reasoning.block.route is OutputRoute.METADATA
    assert reply.block.route is OutputRoute.CONVERSATION
    assert reasoning.block.timestamp == '-'
    assert reply.block.timestamp == '-'

  def test_structural_layouts_produce_renderer_neutral_block_kinds(self):
    renderer = CaptureRenderer()
    configuration = DisplayConfig(
      record_filter=RecordFilter.including(
        RecordKind.TRAIL_METADATA,
        RecordKind.NATIVE_STEP,
        RecordKind.TRAIL_LIST_ROW,
        RecordKind.LINEAGE_NODE,
      )
    )
    with DisplaySession(configuration, renderer) as session:
      session.consume(
        [
          TrailMetadata(
            key='metadata',
            origin=Origin.RECORDED,
            fields=(('harness', 'bro'),),
          ),
          NativeStep(
            key='step',
            origin=Origin.RECORDED,
            step_id=1,
            step_kind='user_input',
            body=InlineStepBody('hello'),
          ),
          TrailListRow(
            key='row',
            origin=Origin.RECORDED,
            trail_id='trail',
            harness='bro',
            owner='dev',
            model='gpt',
            status='live',
          ),
          LineageNode(
            key='node',
            origin=Origin.RECORDED,
            trail_id='trail',
            depth=0,
            is_last=True,
          ),
        ]
      )
    assert [
      operation.block.kind for operation in renderer.operations if isinstance(operation, Append)
    ] == [
      BlockKind.METADATA,
      BlockKind.NATIVE_STEP,
      BlockKind.TRAIL_ROW,
      BlockKind.LINEAGE_NODE,
    ]

  def test_empty_assistant_and_reasoning_are_visible_only_in_debug_detail(self):
    records = [
      Reasoning(
        key='reasoning',
        origin=Origin.LIVE,
        source=LiveSource('run', 1),
        content='  ',
      ),
      AssistantText(
        key='assistant',
        origin=Origin.LIVE,
        source=LiveSource('run', 2),
        content='',
      ),
    ]
    normal = CaptureRenderer()
    with DisplaySession(DisplayConfig(), normal) as session:
      session.consume(records)
    debug = CaptureRenderer()
    with DisplaySession(DisplayConfig(verbosity=Verbosity.DEBUG), debug) as session:
      session.consume(records)
    assert normal.operations == []
    assert len(debug.operations) == 2

  def test_transient_activity_uses_append_update_remove(self):
    renderer = CaptureRenderer()
    with DisplaySession(DisplayConfig(), renderer) as session:
      session.consume(
        [
          TransientActivity(
            key='one', origin=Origin.SURFACE, activity_id='work', content='thinking'
          ),
          TransientActivity(
            key='two', origin=Origin.SURFACE, activity_id='work', content='calling'
          ),
          TransientActivity(
            key='three',
            origin=Origin.SURFACE,
            activity_id='work',
            content='',
            active=False,
          ),
        ]
      )
    assert [type(operation) for operation in renderer.operations] == [Append, Update, Remove]

  def test_replay_and_batched_follow_produce_the_same_retained_document(self):
    records = [_live_call(), _live_result()]
    replay = RetainedRenderer()
    with DisplaySession(DisplayConfig(color=ColorMode.NEVER), replay) as session:
      session.consume(records)
    followed = RetainedRenderer()
    with DisplaySession(DisplayConfig(color=ColorMode.NEVER), followed) as session:
      session.consume(records[:1])
      session.consume(records[1:])
    assert replay.document() == followed.document()

  def test_session_rejects_use_from_another_thread(self):
    renderer = CaptureRenderer()
    session = DisplaySession(DisplayConfig(), renderer)
    failures: list[Exception] = []

    def consume_elsewhere() -> None:
      try:
        session.consume(_live_call())
      except Exception as exception:
        failures.append(exception)

    thread = threading.Thread(target=consume_elsewhere)
    thread.start()
    thread.join()
    session.close()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert 'owning thread' in str(failures[0])
