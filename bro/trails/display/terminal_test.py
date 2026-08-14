import io
from typing import TextIO, cast

import pytest

from bro.trails.display import (
  Append,
  BlockItem,
  BlockKind,
  ColorMode,
  DisplayConfig,
  Layout,
  OutputRoute,
  PresentationBlock,
  Remove,
  RetainedRenderer,
  StreamRenderer,
  StyleRole,
  Update,
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
      OutputRoute.REPLY: reply,
      OutputRoute.TRACE: trace,
    },
    environment={},
  )
  renderer.start(DisplayConfig(color=ColorMode.AUTO))
  renderer.apply(
    Append(
      _block(
        'reply',
        BlockItem('answer', style=StyleRole.ASSISTANT),
        route=OutputRoute.REPLY,
        style=StyleRole.ASSISTANT,
      )
    )
  )
  renderer.apply(
    Append(
      _block(
        'trace',
        BlockItem('activity', style=StyleRole.TOOL),
        route=OutputRoute.TRACE,
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
  renderer.start(DisplayConfig(color=ColorMode.NEVER))
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
        timestamp=None,
        items=(BlockItem('live', label='status'),),
      )
    )
  )
  document = renderer.document()
  assert '    └── trail-child' in document
  assert 'owner=dev' in document
  assert 'trail-row  status=live' in document
