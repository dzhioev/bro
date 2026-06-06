"""shared formatting helpers for `call`'s trace renderers.

both `TextRenderer` (in `do/call.py`) and `TUIRenderer` (in `do/call_tui.py`)
render the same kinds of payloads — short reasoning summaries, JSON tool
arguments, JSON-or-text tool results — into single-line strings. the
truncation limit is per-caller (text mode runs alongside reply lines, the
TUI sits inside a narrower bubble column), so it stays on the call site.
"""

import json
from typing import Any


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


def truncate(text: str, limit: int, overflow_marker: bool = True) -> str:
  """cap `text` to `limit` chars; when `overflow_marker` is set, append
  `… <N more chars>` so the reader can tell how much was dropped. set it
  False for the TUI variant where the bubble is already narrow."""
  if len(text) <= limit:
    return text
  if overflow_marker:
    overflow = len(text) - limit
    return f'{text[:limit]}… <{overflow} more chars>'
  return f'{text[:limit]}…'
