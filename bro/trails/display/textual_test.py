import pytest
from textual.app import App, ComposeResult
from textual.selection import SELECT_ALL

from bro.trails.display import (
  Append,
  BlockItem,
  BlockKind,
  Layout,
  OutputRoute,
  PresentationBlock,
  Remove,
  StyleRole,
  Update,
  preset,
)
from bro.trails.display.textual import (
  BubbleRow,
  DateSeparator,
  MessageBubble,
  SystemBubble,
  TextualRenderer,
  TrailView,
  TypingIndicator,
)


class _RendererApp(App):
  def compose(self) -> ComposeResult:
    yield TrailView(id='trail')


def _block(
  block_id: str,
  kind: BlockKind,
  *items: BlockItem,
  style: StyleRole = StyleRole.ASSISTANT,
  timestamp: str | None = '12:34:56',
  calendar_date: str | None = '2026-08-15',
) -> PresentationBlock:
  return PresentationBlock(
    id=block_id,
    kind=kind,
    layout=Layout.CONVERSATION,
    route=OutputRoute.CONVERSATION,
    style=style,
    label='assistant',
    timestamp=timestamp,
    calendar_date=calendar_date,
    items=items,
  )


def test_textual_renderer_declares_retained_interactive_markdown_capabilities():
  capabilities = TextualRenderer.capabilities
  assert capabilities.retained_updates
  assert capabilities.removal
  assert capabilities.markdown
  assert capabilities.interactive


@pytest.mark.asyncio
async def test_textual_renderer_contract_updates_and_removes_in_place():
  app = _RendererApp()
  async with app.run_test(size=(80, 30)) as pilot:
    renderer = TextualRenderer(app.query_one(TrailView))
    renderer.start(preset('call'))
    first = _block(
      'message',
      BlockKind.MESSAGE,
      BlockItem('**working**', markdown=True),
    )
    updated = _block(
      'message',
      BlockKind.MESSAGE,
      BlockItem('**done**', markdown=True),
    )
    status = _block(
      'status',
      BlockKind.STATUS,
      BlockItem('thinking'),
      style=StyleRole.MUTED,
      timestamp=None,
      calendar_date=None,
    )

    renderer.apply(Append(first))
    renderer.apply(Append(status))
    await pilot.pause()
    row = app.query_one(BubbleRow)
    renderer.apply(Update(updated))
    renderer.apply(Remove('status'))
    await pilot.pause()

    assert app.query_one(BubbleRow) is row
    assert len(app.query(TypingIndicator)) == 0
    assert len(app.query(DateSeparator)) == 1
    bubble = row.query_one(MessageBubble)
    app.screen.selections = {bubble: SELECT_ALL}
    assert app.screen.get_selected_text() == 'done'
    renderer.close()


@pytest.mark.asyncio
async def test_textual_tool_payload_is_plain_selectable_content():
  app = _RendererApp()
  async with app.run_test(size=(100, 20)) as pilot:
    renderer = TextualRenderer(app.query_one(TrailView))
    renderer.start(preset('call'))
    payload = "[x=1, statuses=['done', 'dropped']]"
    renderer.apply(
      Append(
        _block(
          'tool',
          BlockKind.TOOL,
          BlockItem(payload, label='arguments'),
          style=StyleRole.TOOL,
        )
      )
    )
    await pilot.pause()

    system = app.query_one(SystemBubble)
    app.screen.selections = {system: SELECT_ALL}
    assert app.screen.get_selected_text() == f'→ assistant({payload})'
    renderer.close()
