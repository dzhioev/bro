import io
from typing import TextIO, cast

import pytest

from bro.trails.display import (
  Appearance,
  Append,
  AssistantText,
  BlockItem,
  BlockKind,
  ColorMode,
  DisplayConfig,
  DisplaySession,
  Layout,
  LiveSource,
  Origin,
  OutputRoute,
  PresentationBlock,
  Reasoning,
  RecordedSource,
  Remove,
  RetainedRenderer,
  StreamRenderer,
  StyleRole,
  ToolCall,
  ToolResult,
  TrailMetadata,
  Update,
  UserInput,
  preset,
)


class TTYBuffer(io.StringIO):
  def isatty(self) -> bool:
    return True


class BrokenStream:
  def write(self, text: str) -> int:
    raise BrokenPipeError

  def flush(self) -> None:
    raise BrokenPipeError

  def isatty(self) -> bool:
    return False


def _block(
  block_id: str,
  *items: BlockItem,
  route: OutputRoute = OutputRoute.TRACE,
  style: StyleRole = StyleRole.NORMAL,
) -> PresentationBlock:
  return PresentationBlock(
    id=block_id,
    kind=BlockKind.EVENT,
    layout=Layout.EVENT_LOG,
    route=route,
    style=style,
    label=block_id,
    timestamp=None,
    items=items,
  )


def test_observer_preset_uses_the_plain_log_shape():
  stream = io.StringIO()
  renderer = StreamRenderer(stream)
  configuration = preset('observer', context_label='test-bro')
  with DisplaySession(configuration, renderer) as session:
    session.consume(
      [
        Reasoning(
          key='reasoning',
          origin=Origin.LIVE,
          source=LiveSource('run', 1),
          timestamp='12:34:56',
          content='thinking',
        ),
        ToolCall(
          key='call',
          origin=Origin.LIVE,
          source=LiveSource('run', 2),
          timestamp='12:34:57',
          call_id='call',
          tool_name='repo__search',
          arguments={'query': 'x'},
        ),
        ToolResult(
          key='result',
          origin=Origin.LIVE,
          source=LiveSource('run', 3),
          timestamp='12:34:58',
          call_id='call',
          result='ok',
        ),
      ]
    )
  assert stream.getvalue() == (
    '[12:34:56] test-bro reasoning\n'
    '  thinking\n'
    '\n'
    '[12:34:57] test-bro tool call: repo::search\n'
    '  {\n'
    '    "query": "x"\n'
    '  }\n'
    '\n'
    '[12:34:58] test-bro tool result: repo::search\n'
    '  ok\n'
    '\n'
  )


def test_ask_preset_keeps_the_reply_channel_undecorated():
  reply = io.StringIO()
  trace = io.StringIO()
  renderer = StreamRenderer(
    {
      OutputRoute.REPLY: reply,
      OutputRoute.TRACE: trace,
    }
  )
  with DisplaySession(preset('ask', context_label='dev'), renderer) as session:
    session.consume(
      AssistantText(
        key='reply',
        origin=Origin.LIVE,
        source=LiveSource('run', 1),
        timestamp='12:34:56',
        content='answer',
      )
    )
  assert reply.getvalue() == 'answer\n'
  assert trace.getvalue() == ''


def test_chat_preset_uses_timestamped_conversation_lines():
  stream = io.StringIO()
  renderer = StreamRenderer(stream)
  configuration = preset('call', context_label='dev')
  with DisplaySession(configuration, renderer) as session:
    session.consume(
      [
        Reasoning(
          key='reasoning',
          origin=Origin.LIVE,
          source=LiveSource('run', 1),
          timestamp='2026-01-01T12:34:56Z',
          content='one\n two',
        ),
        ToolCall(
          key='call',
          origin=Origin.LIVE,
          source=LiveSource('run', 2),
          timestamp='2026-01-01T12:34:57Z',
          call_id='call',
          tool_name='repo__search',
          arguments={'query': 'x', 'sentence': 'more than ten characters'},
        ),
        ToolResult(
          key='result',
          origin=Origin.LIVE,
          source=LiveSource('run', 3),
          timestamp='2026-01-01T12:34:58Z',
          call_id='call',
          result={'hidden': True},
        ),
        AssistantText(
          key='reply',
          origin=Origin.LIVE,
          source=LiveSource('run', 4),
          timestamp='2026-01-01T12:34:59Z',
          content='done',
        ),
      ]
    )
  assert stream.getvalue() == (
    '[12:34:56] dev · thinking: one two\n'
    '[12:34:57] dev → repo::search(query=x, sentence=...)\n'
    '[12:34:58] dev ← repo::search\n'
    '[12:34:59] dev: done\n'
  )


def test_rewind_preset_uses_numbered_conversation_turns():
  renderer = RetainedRenderer()
  configuration = preset('rewind-show', color=ColorMode.NEVER)
  with DisplaySession(configuration, renderer) as session:
    session.consume(
      [
        TrailMetadata(
          key='metadata',
          origin=Origin.RECORDED,
          fields=(('trail', 'T1'), ('harness', 'bro')),
        ),
        UserInput(
          key='user',
          origin=Origin.RECORDED,
          source=RecordedSource('T1', 1),
          timestamp='2026-01-01T00:00:01Z',
          content='hello',
        ),
        Reasoning(
          key='reasoning',
          origin=Origin.RECORDED,
          source=RecordedSource('T1', 2, 1),
          timestamp='2026-01-01T00:00:02Z',
          content='think',
        ),
        AssistantText(
          key='answer',
          origin=Origin.RECORDED,
          source=RecordedSource('T1', 2, 2),
          timestamp='2026-01-01T00:00:02Z',
          content='working',
        ),
        ToolCall(
          key='call',
          origin=Origin.RECORDED,
          source=RecordedSource('T1', 2, 3),
          timestamp='2026-01-01T00:00:02Z',
          call_id='call',
          tool_name='search',
          arguments={'query': 'x'},
        ),
        ToolResult(
          key='result',
          origin=Origin.RECORDED,
          source=RecordedSource('T1', 3),
          timestamp='2026-01-01T00:00:03Z',
          call_id='call',
          result='found',
        ),
      ]
    )
  assert renderer.document() == (
    'trail   T1\n'
    'harness bro\n'
    '──────────────────────────────────────────────────────────────────────────────\n'
    '\n#1 USER 2026-01-01 00:00:01\n'
    '  hello\n'
    '\n#2 ASSISTANT 2026-01-01 00:00:02\n'
    '  [thinking]\n'
    '    think\n'
    '  working\n'
    '  → search({"query": "x"})\n'
    '    found\n'
  )


@pytest.mark.parametrize('renderer_kind', ['stream', 'retained'])
def test_renderer_contract_accepts_append_update_remove(renderer_kind: str):
  stream = io.StringIO()
  renderer = StreamRenderer(stream) if renderer_kind == 'stream' else RetainedRenderer()
  renderer.start(DisplayConfig(color=ColorMode.NEVER))
  first = _block('a', BlockItem('one'))
  updated = _block('a', BlockItem('one'), BlockItem('two'))
  removable = _block('b', BlockItem('gone'))
  renderer.apply(Append(first))
  renderer.apply(Update(updated))
  renderer.apply(Append(removable))
  renderer.apply(Remove('b'))
  renderer.close()

  if isinstance(renderer, StreamRenderer):
    assert not renderer.capabilities.retained_updates
    assert not renderer.capabilities.removal
    assert stream.getvalue().count('\n  one\n') == 1
    assert stream.getvalue().count('\n  two\n') == 1
    assert 'gone' in stream.getvalue()
  else:
    assert renderer.capabilities.retained_updates
    assert renderer.capabilities.removal
    assert 'one' in renderer.document()
    assert 'two' in renderer.document()
    assert 'gone' not in renderer.document()


def test_renderers_fail_fast_on_invalid_operation_sequences():
  renderer = RetainedRenderer()
  renderer.start(DisplayConfig())
  block = _block('missing', BlockItem('x'))
  with pytest.raises(ValueError, match='unknown block'):
    renderer.apply(Update(block))
  renderer.apply(Append(block))
  with pytest.raises(ValueError, match='duplicate block'):
    renderer.apply(Append(block))


def test_stream_routes_and_resolves_auto_color_per_destination():
  reply = TTYBuffer()
  trace = io.StringIO()
  renderer = StreamRenderer(
    {
      OutputRoute.TRACE: reply,
      OutputRoute.METADATA: trace,
    },
    environment={},
  )
  renderer.start(DisplayConfig(color=ColorMode.AUTO))
  renderer.apply(
    Append(
      _block(
        'reply',
        BlockItem('answer', style=StyleRole.ASSISTANT),
        route=OutputRoute.TRACE,
        style=StyleRole.ASSISTANT,
      )
    )
  )
  renderer.apply(
    Append(
      _block(
        'trace',
        BlockItem('activity', style=StyleRole.TOOL),
        route=OutputRoute.METADATA,
        style=StyleRole.TOOL,
      )
    )
  )
  renderer.close()
  assert '\x1b[' in reply.getvalue()
  assert '\x1b[' not in trace.getvalue()


def test_no_color_disables_automatic_color_on_a_tty():
  stream = TTYBuffer()
  renderer = StreamRenderer(stream, environment={'NO_COLOR': ''})
  renderer.start(DisplayConfig(color=ColorMode.AUTO))
  renderer.apply(Append(_block('notice', BlockItem('hello'), style=StyleRole.NOTICE)))
  renderer.close()
  assert '\x1b[' not in stream.getvalue()


def test_always_color_overrides_no_color():
  stream = io.StringIO()
  renderer = RetainedRenderer(target=stream, environment={'NO_COLOR': ''})
  renderer.start(DisplayConfig(color=ColorMode.ALWAYS))
  renderer.apply(Append(_block('error', BlockItem('bad'), style=StyleRole.ERROR)))
  assert '\x1b[' in renderer.document()


def test_broken_pipe_is_a_normal_stream_consumer_close():
  stream = cast(TextIO, BrokenStream())
  renderer = StreamRenderer(stream)
  renderer.start(DisplayConfig())
  renderer.apply(Append(_block('event', BlockItem('content'))))
  renderer.apply(Append(_block('later', BlockItem('ignored'))))
  renderer.close()
  assert renderer.consumer_closed


def test_retained_renderer_exposes_route_documents_and_final_blocks():
  renderer = RetainedRenderer()
  renderer.start(DisplayConfig(color=ColorMode.NEVER))
  renderer.apply(Append(_block('reply', BlockItem('answer'), route=OutputRoute.REPLY)))
  renderer.apply(Append(_block('trace', BlockItem('activity'), route=OutputRoute.TRACE)))
  assert 'answer' in renderer.document(OutputRoute.REPLY)
  assert 'activity' not in renderer.document(OutputRoute.REPLY)
  assert [block.id for block in renderer.blocks] == ['reply', 'trace']


def test_retained_tree_and_list_layouts_are_plain_terminal_documents():
  renderer = RetainedRenderer()
  renderer.start(
    DisplayConfig(
      color=ColorMode.NEVER,
      appearance=Appearance.REWIND,
      layout=Layout.LINEAGE_TREE,
    )
  )
  renderer.apply(
    Append(
      PresentationBlock(
        id='tree',
        kind=BlockKind.LINEAGE_NODE,
        layout=Layout.LINEAGE_TREE,
        route=OutputRoute.METADATA,
        style=StyleRole.METADATA,
        label='trail-child',
        timestamp=None,
        items=(BlockItem('dev', label='owner'),),
        depth=1,
        tree_last=True,
      )
    )
  )
  renderer.apply(
    Append(
      PresentationBlock(
        id='row',
        kind=BlockKind.TRAIL_ROW,
        layout=Layout.TRAIL_LIST,
        route=OutputRoute.METADATA,
        style=StyleRole.METADATA,
        label='trail-row',
        timestamp='2026-01-01 00:00:00',
        items=(
          BlockItem('bro', label='harness'),
          BlockItem('dev', label='owner'),
          BlockItem('gpt', label='model'),
          BlockItem('live', label='status'),
        ),
      )
    )
  )
  document = renderer.document()
  assert '    └── trail-child' in document
  assert 'trail-child  dev/?' in document
  assert 'trail-row  2026-01-01 00:00:00  bro' in document
  assert 'live' in document
