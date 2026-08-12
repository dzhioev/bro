"""shared formatting helpers for `call`'s trace renderers.

both `TextRenderer` (in `call.py`) and `TUIRenderer` (in `call_tui.py`) render
the same trace events. tool calls go through `format_tool_call` — one canonical
`namespace::tool(arg=value, …)` line, identical in both surfaces. text mode
additionally collapses a reasoning summary onto a single line and caps it with
`truncate`, whose limit stays on the call site.
"""

import json
from typing import Any

from bro.llm.mcp import canonical_name


def oneline(text: str) -> str:
  # collapse arbitrary whitespace runs into single spaces so the rendered
  # event fits on one line without breaking the surrounding column.
  return ' '.join(text.split())


def compact_value(value: dict[str, Any] | str) -> str:
  """render a tool argument or result as a single compact line.

  dicts go through `json.dumps` with no whitespace. strings that look like
  JSON (start with `{` or `[`) are reparsed and re-emitted compactly; anything
  else falls through `oneline`.
  """
  if isinstance(value, dict):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
  text = value.strip()
  if len(text) > 0 and text[0] in '{[':
    try:
      parsed = json.loads(text)
    except json.JSONDecodeError:
      return oneline(text)
    return json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
  return oneline(text)


def truncate(text: str, limit: int) -> str:
  """cap `text` to `limit` chars, appending `… <N more chars>` so the reader
  can tell how much was dropped."""
  if len(text) <= limit:
    return text
  overflow = len(text) - limit
  return f'{text[:limit]}… <{overflow} more chars>'


# an argument value is shown inline only when its compact form is short; a longer
# one (a file path, a pasted prompt) is replaced with `...` so it can't swamp the
# single-line trace.
ARGUMENT_VALUE_LIMIT = 10


def format_tool_call(name: str, arguments: dict[str, Any]) -> str:
  """render a tool call as `namespace::tool(arg=value, …)`: the canonical tool
  name, every argument named, and each value elided to `...` once its compact
  form passes ARGUMENT_VALUE_LIMIT chars."""
  rendered = ', '.join(f'{key}={_argument_value(value)}' for key, value in arguments.items())
  return f'{canonical_name(name)}({rendered})'


def _argument_value(value: Any) -> str:
  if isinstance(value, (dict, str)):
    compact = compact_value(value)
  else:
    compact = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
  return compact if len(compact) <= ARGUMENT_VALUE_LIMIT else '...'
