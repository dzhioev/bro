"""Windowed views over large text for tool output.

`apply_limit` caps free-form output to a line budget, and to a byte budget where
its caller passes one, keeping the head or tail and announcing what was dropped
via inline `[...skipped before/after...]` markers; `numbered_window` layers a
cat -n-numbered partial read on top, skipping `offset` lines and closing on the
offset that reads on (`numbered` numbers a whole text); `take_head` returns the
budget-bounded prefix raw, for callers that paginate over a cursor instead of
dropping the excess.
"""

from typing import Literal, Optional

# callers pass `limit=N` lines to extend the default, up to MAX_LIMIT, clamped
# beyond with the clamp announced.
DEFAULT_LIMIT = 100
MAX_LIMIT = 2000
# the cap on one tool reply and on one batch of session news. ~30 KB is well
# under OpenAI's 10 MB per-tool-output limit and cheap on input tokens across
# the agent loop.
BYTE_LIMIT = 30_000


def format_size(byte_count: int) -> str:
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
    segments.append(format_size(byte_count))
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


def _take(lines: list[str], effective: int, byte_limit: Optional[int]) -> list[str]:
  kept: list[str] = []
  kept_bytes = 0
  for line in lines:
    if len(kept) >= effective:
      break
    if byte_limit is not None and kept_bytes + len(line) > byte_limit:
      break
    kept.append(line)
    kept_bytes += len(line)
  return kept


def _cut(content: str, keep: Literal['head', 'tail'], byte_limit: int) -> str:
  """the widest slice of a line too wide to keep whole, so a window always
  carries content and a cursor always advances."""
  return content[:byte_limit] if keep == 'head' else content[-byte_limit:]


def _joined(*notes: str) -> str:
  return '; '.join(note for note in notes if len(note) > 0)


def apply_limit(
  content: str,
  limit: int,
  *,
  keep: Literal['head', 'tail'] = 'head',
  byte_limit: Optional[int] = None,
  skipped_before_lines: int = 0,
  skipped_before_bytes: int = 0,
  skipped_after_lines: int = 0,
  skipped_after_bytes: int = 0,
  after_note: str = '',
) -> str:
  """cap content to `limit` lines, and to `byte_limit` bytes when given, keeping
  the head or tail. Wraps the kept slice with `[...skipped before/after...]`
  markers reporting what was dropped at each end, including content a streaming
  caller counted without retaining and supplies through the skipped arguments;
  `after_note` rides the after marker when one renders."""
  effective, clamp_note = _clamp(limit)
  lines = content.splitlines(keepends=True)
  total_lines = len(lines)
  total_bytes = len(content)

  source = list(reversed(lines)) if keep == 'tail' else lines
  kept = _take(source, effective, byte_limit)
  if len(kept) == 0 and len(content) > 0:
    assert byte_limit is not None  # only a byte budget can refuse the first line
    kept = [_cut(content, keep, byte_limit)]
  kept_bytes = sum(len(line) for line in kept)
  if keep == 'tail':
    kept.reverse()

  dropped_lines = total_lines - len(kept)
  dropped_bytes = total_bytes - kept_bytes

  if keep == 'head':
    before_lines, before_bytes = skipped_before_lines, skipped_before_bytes
    after_lines = dropped_lines + skipped_after_lines
    after_bytes = dropped_bytes + skipped_after_bytes
    before_note, after_clamp_note = '', clamp_note
  else:
    before_lines = skipped_before_lines + dropped_lines
    before_bytes = skipped_before_bytes + dropped_bytes
    after_lines, after_bytes = skipped_after_lines, skipped_after_bytes
    before_note, after_clamp_note = clamp_note, ''

  pieces: list[str] = []
  if before_lines > 0 or before_bytes > 0 or len(before_note) > 0:
    pieces.append(_marker('before', before_lines, before_bytes, note=before_note))
  body = ''.join(kept).rstrip('\n')
  if len(body) > 0:
    pieces.append(body)
  if after_lines > 0 or after_bytes > 0 or len(after_clamp_note) > 0:
    dropped = after_lines > 0 or after_bytes > 0
    after = _joined(after_clamp_note, after_note) if dropped else after_clamp_note
    pieces.append(_marker('after', after_lines, after_bytes, note=after))
  return '\n'.join(pieces)


def take_head(content: str, limit: int = MAX_LIMIT) -> tuple[str, str]:
  """the head of `content` within the `limit` line and `BYTE_LIMIT` budget
  (clamped), returned as a raw prefix for cursor-style pagination: the caller
  advances a cursor by the returned length and serves the remainder on later
  calls, so unlike `apply_limit` nothing is dropped. Keeps whole lines while they
  fit; when the first line alone exceeds the byte budget it is cut mid-line so
  the cursor always makes progress. Returns (kept_prefix, clamp_note)."""
  effective, clamp_note = _clamp(limit)
  kept = _take(content.splitlines(keepends=True), effective, BYTE_LIMIT)
  if len(kept) == 0 and len(content) > 0:
    return (_cut(content, 'head', BYTE_LIMIT), clamp_note)
  return (''.join(kept), clamp_note)


def numbered(content: str, *, start: int = 1) -> str:
  """`content` with each line prefixed by its 1-based number (cat -n style),
  counting from `start`."""
  return ''.join(
    f'{index:>5}\t{line}'
    for index, line in enumerate(content.splitlines(keepends=True), start=start)
  )


def numbered_window(content: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
  """oriented partial read: skip `offset` lines (0-based), number the rest, and
  cap them to `limit` lines; the after marker names the offset that reads on."""
  all_lines = content.splitlines(keepends=True)
  before_count = min(max(offset, 0), len(all_lines))
  before_bytes = sum(len(line) for line in all_lines[:before_count])
  effective, _ = _clamp(limit)
  return apply_limit(
    numbered(''.join(all_lines[before_count:]), start=before_count + 1),
    limit,
    keep='head',
    skipped_before_lines=before_count,
    skipped_before_bytes=before_bytes,
    after_note=f'read on with offset={before_count + effective}',
  )
