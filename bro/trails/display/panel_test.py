import subprocess
import sys
from io import StringIO

from bro.trails.display.config import Layout, OutputRoute, PresetName, preset
from bro.trails.display.core import DisplaySession
from bro.trails.display.panel import RichPanelRenderer
from bro.trails.display.records import (
  AssistantText,
  LiveSource,
  Origin,
  Reasoning,
  ToolCall,
  ToolResult,
)


def _common(sequence: int) -> dict:
  return {
    'key': f'live:run-1:{sequence}',
    'origin': Origin.LIVE,
    'source': LiveSource('run-1', sequence),
    'timestamp': '2026-08-15T01:02:03+00:00',
  }


def test_panel_renderer_routes_undecorated_reply_away_from_activity():
  stdout = StringIO()
  stderr = StringIO()
  destinations = dict.fromkeys(OutputRoute, stderr)
  destinations[OutputRoute.REPLY] = stdout
  configuration = preset(PresetName.ASK, layout=Layout.PANELS, context_label='bro')

  with DisplaySession(configuration, RichPanelRenderer(destinations)) as session:
    session.consume(Reasoning(content='thinking', **_common(0)))
    session.consume(AssistantText(content='answer', **_common(1)))

  assert stdout.getvalue() == 'answer\n'
  activity = stderr.getvalue()
  assert 'thinking' in activity
  assert 'reasoning' in activity
  assert 'answer' not in activity


def test_panel_renderer_turns_tool_updates_into_result_panels():
  output = StringIO()
  configuration = preset(PresetName.OBSERVER, layout=Layout.PANELS)

  with DisplaySession(configuration, RichPanelRenderer(output)) as session:
    session.consume(
      ToolCall(
        call_id='call-1',
        tool_name='service__tool',
        arguments={'value': 1},
        **_common(0),
      )
    )
    session.consume(
      ToolResult(
        call_id='call-1',
        tool_name='service__tool',
        result='done',
        **_common(1),
      )
    )

  rendered = output.getvalue()
  assert 'tool call' in rendered
  assert 'service::tool' in rendered
  assert 'tool result' in rendered
  assert 'done' in rendered


def test_importing_panel_module_does_not_import_rich():
  code = "import sys; import bro.trails.display.panel; assert 'rich' not in sys.modules"
  subprocess.run([sys.executable, '-c', code], check=True)
