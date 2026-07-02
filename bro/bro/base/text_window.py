"""Windowed views over large text for tool output.

Two entry points share one cap policy: `apply_limit` caps free-form output
(keeping the head or tail) and announces what was dropped via inline
`[...skipped before/after...]` markers; `numbered_window` layers an oriented
partial read on top — skip `offset` lines, prefix the rest with 1-based
line numbers (cat -n style), then cap.
"""

# default cap on output lines. Callers can pass `limit=N` to extend, up to
# MAX_LIMIT (silently clamped beyond). The byte budget is `limit * _BYTES_PER_LINE`,
# so the default is ~15 KB and the ceiling is ~300 KB — well under OpenAI's
# 10 MB per-tool-output limit and cheap on input tokens across the agent loop.
DEFAULT_LIMIT = 100
MAX_LIMIT = 2000
_BYTES_PER_LINE = 150


def _format_size(byte_count: int) -> str:
  if byte_count >= 1_000_000:
    return f'{byte_count / 1_000_000:.1f} MB'
  if byte_count >= 1_000:
    return f'{byte_count / 1_000:.1f} KB'
  return f'{byte_count} B'


def _marker(side: str, lines: int, byte_count: int, *, note: str = '') -> str:
  segments: list[str] = []
  if lines > 0:
    segments.append(f'{lines:,} lines')
  if byte_count > 0:
    segments.append(_format_size(byte_count))
  if len(segments) == 0:
    # nothing was actually skipped — the marker exists only to surface `note`
    # (e.g., a clamp warning). Drop the "skipped X: 0" framing entirely.
    return f'[...{note}...]'
  body = ' / '.join(segments)
  suffix = f' — {note}' if len(note) > 0 else ''
  return f'[...skipped {side}: {body}{suffix}...]'


def _clamp(limit: int) -> tuple[int, str]:
  if limit > MAX_LIMIT:
    return MAX_LIMIT, f'limit {limit:,} clamped to {MAX_LIMIT:,}'
  if limit < 1:
    return 1, f'limit {limit} clamped to 1'
  return limit, ''


def apply_limit(
  content: str,
  limit: int,
  *,
  keep: str = 'head',
  skipped_before_lines: int = 0,
  skipped_before_bytes: int = 0,
) -> str:
  """cap content to `limit` lines and `limit * _BYTES_PER_LINE` bytes, keeping
  the head or tail. Wraps the kept slice with `[...skipped before/after...]`
  markers reporting what was dropped at each end (including any prior offset
  surfaced via skipped_before_*)."""
  effective, clamp_note = _clamp(limit)
  byte_budget = effective * _BYTES_PER_LINE
  lines = content.splitlines(keepends=True)
  total_lines = len(lines)
  total_bytes = len(content)

  source = list(reversed(lines)) if keep == 'tail' else lines
  kept: list[str] = []
  kept_bytes = 0
  for line in source:
    if len(kept) >= effective or kept_bytes + len(line) > byte_budget:
      break
    kept.append(line)
    kept_bytes += len(line)
  if keep == 'tail':
    kept.reverse()

  dropped_lines = total_lines - len(kept)
  dropped_bytes = total_bytes - kept_bytes

  if keep == 'head':
    before_lines, before_bytes = skipped_before_lines, skipped_before_bytes
    after_lines, after_bytes = dropped_lines, dropped_bytes
    before_note, after_note = '', clamp_note
  else:
    before_lines = skipped_before_lines + dropped_lines
    before_bytes = skipped_before_bytes + dropped_bytes
    after_lines = after_bytes = 0
    before_note, after_note = clamp_note, ''

  pieces: list[str] = []
  if before_lines > 0 or before_bytes > 0 or len(before_note) > 0:
    pieces.append(_marker('before', before_lines, before_bytes, note=before_note))
  body = ''.join(kept).rstrip('\n')
  if len(body) > 0:
    pieces.append(body)
  if after_lines > 0 or after_bytes > 0 or len(after_note) > 0:
    pieces.append(_marker('after', after_lines, after_bytes, note=after_note))
  return '\n'.join(pieces)


def numbered_window(content: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
  """oriented partial read: skip `offset` lines (0-based), prefix the rest with
  1-based line numbers (cat -n style), and cap via `apply_limit` — the before
  marker reports the skipped prefix."""
  all_lines = content.splitlines(keepends=True)
  before_count = min(max(offset, 0), len(all_lines))
  before_bytes = sum(len(line) for line in all_lines[:before_count])
  visible = all_lines[before_count:]
  numbered = ''.join(f'{i:>5}\t{line}' for i, line in enumerate(visible, start=before_count + 1))
  return apply_limit(
    numbered,
    limit,
    keep='head',
    skipped_before_lines=before_count,
    skipped_before_bytes=before_bytes,
  )
