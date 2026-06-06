import json
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, TextIO


class Tracer(ABC):
  """observe an LLM run as it streams.

  events arrive in the order the model produces them: reasoning summaries,
  assistant text (interim and terminal), the tool calls themselves, and the
  tool outputs that come back. implementations are free to render, discard, or
  capture them.

  `on_assistant_message`'s `terminal` flag distinguishes mid-stream chatter
  (`terminal=False` — assistant text emitted alongside or between tool calls,
  while the run continues) from the final reply (`terminal=True` — the text
  that's also returned from `LLM.send`). Callers that already render the
  return value themselves can branch on the flag to avoid double-emitting.
  """

  @abstractmethod
  def on_reasoning(self, text: str) -> None: ...

  @abstractmethod
  def on_assistant_message(self, text: str, terminal: bool) -> None: ...

  @abstractmethod
  def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None: ...

  @abstractmethod
  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None: ...


class NullTracer(Tracer):
  """no-op tracer used when nothing should be rendered."""

  def on_reasoning(self, text: str) -> None:
    pass

  def on_assistant_message(self, text: str, terminal: bool) -> None:
    pass

  def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
    pass

  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None:
    pass


_RESULT_LIMIT = 1500
_REASONING_LIMIT = 4000
_MESSAGE_LIMIT = 4000


def _truncate(text: str, limit: int) -> str:
  if len(text) <= limit:
    return text
  overflow = len(text) - limit
  return f'{text[:limit]}\n... <{overflow} more chars>'


def _format_value(value: dict[str, Any] | str, limit: int) -> tuple[str, bool]:
  """render `value` for display. returns (text, is_json)."""
  if isinstance(value, dict):
    return _truncate(json.dumps(value, indent=2, ensure_ascii=False), limit), True
  text = value.strip()
  if len(text) > 0 and text[0] in '{[':
    try:
      parsed = json.loads(text)
    except json.JSONDecodeError:
      pass
    else:
      return _truncate(json.dumps(parsed, indent=2, ensure_ascii=False), limit), True
  return _truncate(value, limit), False


# injectable so tests can pin the clock; production code uses _default_now.
def _default_now() -> str:
  return datetime.now().strftime('%H:%M:%S')


# shared so concurrent tracer calls don't tear each other's panels apart on
# stderr. lazily built so importing this module stays free of `rich` —
# required so deployed images (flow-mcp-server, the emails Lambda) that never
# instantiate a RichConsoleTracer don't need to ship the `rich` package.
_CONSOLE: Any = None


def _get_console() -> Any:
  global _CONSOLE
  if _CONSOLE is None:
    from rich.console import Console

    # `--rich` is an explicit opt-in for the colored panel format; honor it
    # regardless of TTY detection (rich's auto-detection is brittle —
    # subprocess wrappers and unusual $TERM values silently strip colors).
    _CONSOLE = Console(file=sys.stderr, highlight=False, force_terminal=True)
  return _CONSOLE


class RichConsoleTracer(Tracer):
  """render trace events as colored `rich` panels to stderr."""

  def __init__(self, prefix: str = '', console: Any = None, now: Callable[[], str] = _default_now):
    self._prefix = prefix
    self._console = console if console is not None else _get_console()
    self._now = now

  def _title(self, label: str) -> str:
    parts = [f'[{self._now()}]']
    if len(self._prefix) > 0:
      parts.append(self._prefix)
    parts.append(label)
    return ' · '.join(parts)

  def _emit(self, label: str, body: Any, border_style: str) -> None:
    from rich.panel import Panel

    self._console.print(
      Panel(body, title=self._title(label), border_style=border_style, title_align='left')
    )

  def on_reasoning(self, text: str) -> None:
    from rich.text import Text

    body = Text(_truncate(text, _REASONING_LIMIT), style='italic')
    self._emit('reasoning', body, 'magenta')

  def on_assistant_message(self, text: str, terminal: bool) -> None:
    from rich.text import Text

    body = Text(_truncate(text, _MESSAGE_LIMIT))
    # mark terminal panels distinctly so 'this is the answer' stands out from
    # the rare interim narration the model emits between tool calls.
    label = 'reply' if terminal else 'assistant'
    style = 'bright_blue' if terminal else 'blue'
    self._emit(label, body, style)

  def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
    self._emit(f'tool call · {name}', _render_json_or_text(arguments, _RESULT_LIMIT), 'cyan')

  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None:
    self._emit(f'tool result · {name}', _render_json_or_text(result, _RESULT_LIMIT), 'green')


def _render_json_or_text(value: dict[str, Any] | str, limit: int) -> Any:
  from rich.syntax import Syntax
  from rich.text import Text

  rendered, is_json = _format_value(value, limit)
  return Syntax(rendered, 'json', theme='ansi_dark') if is_json else Text(rendered)


class BoringTracer(Tracer):
  """plain-text tracer — no colors, no boxes, no rich dependency.

  each event becomes a timestamped header line followed by an indented body and
  a trailing blank line. JSON values stay pretty-printed so the structure is
  readable when piped to a file or grep.
  """

  def __init__(
    self,
    prefix: str = '',
    file: TextIO | None = None,
    now: Callable[[], str] = _default_now,
  ):
    self._prefix = prefix
    self._file = file if file is not None else sys.stderr
    self._now = now

  def _header(self, label: str) -> str:
    parts = [f'[{self._now()}]']
    if len(self._prefix) > 0:
      parts.append(self._prefix)
    parts.append(label)
    return ' '.join(parts)

  def _emit(self, label: str, body: str) -> None:
    print(self._header(label), file=self._file)
    for line in body.splitlines():
      print(f'  {line}', file=self._file)
    print(file=self._file)
    self._file.flush()

  def on_reasoning(self, text: str) -> None:
    self._emit('reasoning', _truncate(text, _REASONING_LIMIT))

  def on_assistant_message(self, text: str, terminal: bool) -> None:
    self._emit('reply' if terminal else 'assistant', _truncate(text, _MESSAGE_LIMIT))

  def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
    rendered, _ = _format_value(arguments, _RESULT_LIMIT)
    self._emit(f'tool call: {name}', rendered)

  def on_tool_result(self, name: str, result: dict[str, Any] | str) -> None:
    rendered, _ = _format_value(result, _RESULT_LIMIT)
    self._emit(f'tool result: {name}', rendered)
