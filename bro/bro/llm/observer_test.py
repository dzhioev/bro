import io
from typing import Any

from rich.console import Console

from llm.observer import BoringRenderer, NullObserver, RichConsoleRenderer, _format_value, _truncate


class TestTruncate:
  def test_under_limit_returns_unchanged(self):
    assert _truncate('hello', 10) == 'hello'

  def test_exactly_at_limit_returns_unchanged(self):
    assert _truncate('hello', 5) == 'hello'

  def test_over_limit_appends_overflow_marker(self):
    result = _truncate('abcdefghij', 4)
    assert result.startswith('abcd\n')
    assert '<6 more chars>' in result


class TestFormatValue:
  def test_dict_renders_pretty_json(self):
    rendered, is_json = _format_value({'a': 1, 'b': [2, 3]}, 100)
    assert is_json is True
    assert '"a": 1' in rendered
    assert '"b":' in rendered

  def test_string_with_json_prefix_is_pretty_printed(self):
    rendered, is_json = _format_value('{"x":1,"y":2}', 100)
    assert is_json is True
    assert '"x": 1' in rendered
    assert '"y": 2' in rendered

  def test_plain_string_returned_as_is(self):
    rendered, is_json = _format_value('hello world', 100)
    assert is_json is False
    assert rendered == 'hello world'

  def test_truncates_large_dict(self):
    rendered, _ = _format_value({'key': 'x' * 200}, 50)
    assert '<' in rendered and 'more chars>' in rendered

  def test_invalid_json_falls_back_to_raw(self):
    rendered, is_json = _format_value('{not actually json', 100)
    assert is_json is False
    assert rendered == '{not actually json'


class TestNullObserver:
  def test_all_methods_are_noops(self):
    t = NullObserver()
    # just verify they don't raise and return None
    assert t.on_reasoning('x') is None
    assert t.on_assistant_message('x', terminal=False) is None
    assert t.on_assistant_message('x', terminal=True) is None
    assert t.on_tool_call('n', {'a': 1}) is None
    assert t.on_tool_result('n', 'r') is None


_FIXED_NOW = '12:00:00'


def _fixed_now() -> str:
  return _FIXED_NOW


def _render_to_string(events: list[tuple[str, Any]]) -> str:
  buffer = io.StringIO()
  console = Console(file=buffer, width=120, force_terminal=False, highlight=False)
  renderer = RichConsoleRenderer(prefix='test-bro', console=console, now=_fixed_now)
  _replay(renderer, events)
  return buffer.getvalue()


def _replay(renderer, events: list[tuple[str, Any]]) -> None:
  for kind, payload in events:
    if kind == 'reasoning':
      renderer.on_reasoning(payload)
    elif kind == 'message':
      renderer.on_assistant_message(payload, terminal=False)
    elif kind == 'reply':
      renderer.on_assistant_message(payload, terminal=True)
    elif kind == 'tool_call':
      renderer.on_tool_call(payload[0], payload[1])
    elif kind == 'tool_result':
      renderer.on_tool_result(payload[0], payload[1])


class TestRichConsoleRenderer:
  def test_reasoning_panel_includes_prefix_and_text(self):
    out = _render_to_string([('reasoning', 'thinking about the task')])
    assert 'test-bro' in out
    assert 'reasoning' in out
    assert 'thinking about the task' in out

  def test_interim_assistant_message_panel_labeled_assistant(self):
    out = _render_to_string([('message', 'narrating mid-stream')])
    assert 'assistant' in out
    assert 'narrating mid-stream' in out

  def test_terminal_assistant_message_panel_labeled_reply(self):
    out = _render_to_string([('reply', 'here is the answer')])
    assert 'reply' in out
    assert 'here is the answer' in out

  def test_tool_call_panel_pretty_prints_args(self):
    out = _render_to_string([('tool_call', ('add_task', {'name': 'buy milk', 'today': True}))])
    assert 'add_task' in out
    assert 'tool call' in out
    assert 'buy milk' in out

  def test_tool_result_panel_string(self):
    out = _render_to_string([('tool_result', ('get_x', 'plain text result'))])
    assert 'get_x' in out
    assert 'tool result' in out
    assert 'plain text result' in out

  def test_tool_result_panel_dict_pretty_printed(self):
    out = _render_to_string([('tool_result', ('lookup', {'id': 'abc', 'count': 3}))])
    assert 'lookup' in out
    assert '"id"' in out
    assert '"count"' in out

  def test_no_prefix_includes_timestamp_only(self):
    buffer = io.StringIO()
    console = Console(file=buffer, width=120, force_terminal=False, highlight=False)
    renderer = RichConsoleRenderer(console=console, now=_fixed_now)
    renderer.on_reasoning('hi')
    out = buffer.getvalue()
    # title is "[12:00:00] · reasoning" — no bro prefix between them
    assert 'test-bro' not in out
    assert _FIXED_NOW in out
    assert 'reasoning' in out

  def test_timestamp_appears_in_title(self):
    out = _render_to_string([('reasoning', 'x')])
    assert _FIXED_NOW in out


class TestBoringRenderer:
  def _render(self, events: list[tuple[str, Any]]) -> str:
    buffer = io.StringIO()
    renderer = BoringRenderer(prefix='test-bro', file=buffer, now=_fixed_now)
    _replay(renderer, events)
    return buffer.getvalue()

  def test_reasoning_emits_timestamped_header_and_indented_body(self):
    out = self._render([('reasoning', 'thinking it through')])
    assert f'[{_FIXED_NOW}] test-bro reasoning' in out
    assert '  thinking it through' in out
    # trailing blank line after each event for separation
    assert out.endswith('\n\n')

  def test_assistant_message_block(self):
    out = self._render([('message', 'final answer here')])
    assert f'[{_FIXED_NOW}] test-bro assistant' in out
    assert '  final answer here' in out

  def test_tool_call_pretty_prints_json_args_with_indent(self):
    out = self._render([('tool_call', ('add_task', {'name': 'buy milk', 'today': True}))])
    assert f'[{_FIXED_NOW}] test-bro tool call: add_task' in out
    assert '  {' in out
    assert '"name": "buy milk"' in out
    assert '"today": true' in out

  def test_tool_result_plain_text(self):
    out = self._render([('tool_result', ('get_x', 'plain result'))])
    assert f'[{_FIXED_NOW}] test-bro tool result: get_x' in out
    assert '  plain result' in out

  def test_tool_result_dict_pretty_printed(self):
    out = self._render([('tool_result', ('lookup', {'id': 'abc', 'count': 3}))])
    assert '"id": "abc"' in out
    assert '"count": 3' in out

  def test_no_ansi_escape_codes(self):
    out = self._render(
      [
        ('reasoning', 'r'),
        ('tool_call', ('t', {'x': 1})),
        ('tool_result', ('t', {'y': 2})),
        ('message', 'm'),
      ]
    )
    assert '\x1b[' not in out

  def test_multiple_events_separated_by_blank_line(self):
    out = self._render([('reasoning', 'first'), ('reasoning', 'second')])
    # one blank line between events; one trailing blank line at the end
    assert '\n\n[' in out

  def test_no_prefix(self):
    buffer = io.StringIO()
    renderer = BoringRenderer(file=buffer, now=_fixed_now)
    renderer.on_reasoning('hi')
    out = buffer.getvalue()
    assert out.startswith(f'[{_FIXED_NOW}] reasoning\n')
