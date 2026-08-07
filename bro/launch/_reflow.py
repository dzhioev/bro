"""reflow a width-wrapped render back onto its unwrapped lines for copy extraction.

a renderer that wraps content to a display width leaves two artifacts in the rendered
lines: soft line breaks at the wrap points, and padding spaces filling lines out to the
width. `Reflow` aligns each display line with the logical lines — the same content
rendered wide enough that only explicit line breaks remain — so a selection made in
display coordinates extracts logical text: the pieces of a wrapped line rejoin (with the
whitespace the wrap consumed restored from the logical line itself) and padding never
reaches the copy.

alignment is greedy: a display line's stripped content must sit in the current logical
line right after the previously consumed piece (only whitespace in between), or open a
following logical line. content that renders differently at the two widths
(width-decorated content: boxes, horizontal rules, re-laid-out tables) fails the match
and degrades to per-line extraction of the display text as-is; alignment resynchronizes
at the next blank display line by skipping the logical lines to their own next blank.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LineSpan:
  """where a display line's content lives in the logical lines: display characters
  [display_start, display_start + length) equal characters
  [logical_start, logical_start + length) of logical line `logical_index`."""

  logical_index: int
  display_start: int
  logical_start: int
  length: int


class Reflow:
  """alignment of one render's display lines onto its logical lines."""

  def __init__(self, display_lines: list[str], logical_lines: list[str]):
    self.display_lines = display_lines
    self.logical_lines = logical_lines
    self.line_spans = _align(display_lines, logical_lines)

  def extract(self, get_span: Callable[[int], Optional[tuple[int, int]]]) -> str:
    """extract the selected text in logical form.

    `get_span` maps a display line index to the selected character span on that line —
    `None` when the line has no selection, an end of -1 for selected-to-end (the
    contract of textual's `Selection.get_span`).
    """
    parts: list[str] = []
    group_index: Optional[int] = None
    group_start = 0
    group_end = 0

    def flush() -> None:
      nonlocal group_index
      if group_index is not None:
        parts.append(self.logical_lines[group_index][group_start:group_end])
        group_index = None

    for y, line in enumerate(self.display_lines):
      selected = get_span(y)
      if selected is None:
        flush()
        continue
      start, end = selected
      if end == -1:
        end = len(line)
      span = self.line_spans[y]
      if span is None:
        flush()
        parts.append(line[start:end].rstrip())
        continue
      piece_start = min(max(start, span.display_start), span.display_start + span.length)
      piece_end = min(max(end, piece_start), span.display_start + span.length)
      logical_from = span.logical_start + (piece_start - span.display_start)
      logical_to = span.logical_start + (piece_end - span.display_start)
      if group_index == span.logical_index:
        group_end = logical_to
      else:
        flush()
        group_index = span.logical_index
        group_start = logical_from
        group_end = logical_to
    flush()

    # decorative padding renders as blank lines: collapse runs and trim the edges so
    # only content-separating blanks survive
    lines: list[str] = []
    for part in parts:
      if part == '' and (len(lines) == 0 or lines[-1] == ''):
        continue
      lines.append(part)
    while len(lines) > 0 and lines[-1] == '':
      lines.pop()
    return '\n'.join(lines)


def _align(display_lines: list[str], logical_lines: list[str]) -> list[Optional[LineSpan]]:
  spans: list[Optional[LineSpan]] = []
  index = 0  # current logical line
  cursor = 0  # characters of logical_lines[index] consumed by previous display lines
  synced = True
  for line in display_lines:
    content = line.strip()
    if not synced and content == '':
      # resynchronize: skip the logical lines to their own next blank
      while index < len(logical_lines) and logical_lines[index].strip() != '':
        index += 1
      cursor = 0
      synced = True
    if not synced:
      spans.append(None)
      continue
    span, index, cursor = _match(line, content, logical_lines, index, cursor)
    synced = span is not None
    spans.append(span)
  return spans


def _match(
  line: str, content: str, logical_lines: list[str], index: int, cursor: int
) -> tuple[Optional[LineSpan], int, int]:
  """match one display line, returning its span (or None) and the advanced position."""
  if content == '':
    if cursor > 0:
      # a partially consumed logical line must have nothing left but whitespace
      if logical_lines[index][cursor:].strip() != '':
        return None, index, cursor
      index += 1
      cursor = 0
    if index >= len(logical_lines) or logical_lines[index].strip() != '':
      return None, index, cursor
    return LineSpan(index, 0, 0, 0), index + 1, 0
  for _ in range(2):
    if index >= len(logical_lines):
      return None, index, cursor
    logical = logical_lines[index]
    position = logical.find(content, cursor)
    if position >= 0 and logical[cursor:position].strip() == '':
      display_start = len(line) - len(line.lstrip())
      length = len(content)
      cursor = position + length
      if logical[:position].strip() == '':
        # the piece opens the logical line's content: pull the shared leading
        # whitespace (code indentation) into the span
        indent = min(display_start, position)
        display_start -= indent
        position -= indent
        length += indent
      span = LineSpan(index, display_start, position, length)
      if logical[cursor:].strip() == '':
        return span, index + 1, 0
      return span, index, cursor
    if logical[cursor:].strip() != '':
      return None, index, cursor
    # the current logical line is spent; try the next
    index += 1
    cursor = 0
  return None, index, cursor
