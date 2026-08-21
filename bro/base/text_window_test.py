from bro.base.text_window import (
  BYTE_LIMIT,
  DEFAULT_LIMIT,
  MAX_LIMIT,
  _clamp,
  _marker,
  apply_limit,
  numbered_window,
  take_head,
  window,
)

# ─── apply_limit / _marker / _clamp ──────────────────────────────────────────


def test_apply_limit_passes_small_content_unchanged():
  text = 'a\nb\nc\n'
  out = apply_limit(text, limit=DEFAULT_LIMIT, keep='head')
  assert 'skipped' not in out
  assert out.replace('\n', '') == 'abc'


def test_apply_limit_head_truncation_reports_after_only():
  text = ''.join(f'L{i}\n' for i in range(50))
  out = apply_limit(text, limit=10, keep='head')
  assert out.startswith('L0\nL1')
  assert 'L9' in out
  assert 'L10' not in out
  assert 'skipped before' not in out
  assert 'skipped after: 40 lines' in out


def test_apply_limit_tail_truncation_reports_before_only():
  text = ''.join(f'L{i}\n' for i in range(50))
  out = apply_limit(text, limit=10, keep='tail')
  assert 'skipped before: 40 lines' in out
  assert 'skipped after' not in out
  assert 'L49' in out
  assert 'L40' in out
  assert 'L39' not in out


def test_apply_limit_skipped_before_param_carries_into_marker():
  text = ''.join(f'L{i}\n' for i in range(5))
  out = apply_limit(text, limit=10, keep='head', skipped_before_lines=42, skipped_before_bytes=900)
  # before marker reports the upstream offset, even though we kept everything we got
  assert 'skipped before: 42 lines / 900 B' in out
  assert 'skipped after' not in out


def test_apply_limit_cuts_one_huge_line_mid_line():
  huge = 'x' * (BYTE_LIMIT + 5_000) + '\n'
  body, _, marker = apply_limit(huge, limit=DEFAULT_LIMIT, keep='head').partition('\n[...')
  assert body == 'x' * BYTE_LIMIT
  assert 'skipped after: 5.0 KB' in marker


def test_apply_limit_cuts_one_huge_line_mid_line_keeping_the_tail():
  huge = 'x' * (BYTE_LIMIT + 5_000) + '\n'
  marker, _, body = apply_limit(huge, limit=DEFAULT_LIMIT, keep='tail').partition('...]\n')
  assert body == 'x' * (BYTE_LIMIT - 1)
  assert 'skipped before: 5.0 KB' in marker


def test_byte_cap_is_independent_of_the_line_limit():
  content = ''.join('x' * 99 + '\n' for _ in range(MAX_LIMIT))
  assert len(take_head(content, limit=400)[0]) == len(take_head(content, limit=MAX_LIMIT)[0])
  assert len(take_head(content, limit=MAX_LIMIT)[0]) <= BYTE_LIMIT


def test_apply_limit_clamp_note_in_after_marker():
  text = ''.join(f'L{i}\n' for i in range(MAX_LIMIT + 100))
  out = apply_limit(text, limit=MAX_LIMIT + 5_000, keep='head')
  assert f'limit {MAX_LIMIT + 5_000:,} clamped to {MAX_LIMIT:,}' in out


def test_apply_limit_clamp_note_in_before_marker_for_tail_keep():
  text = ''.join(f'L{i}\n' for i in range(MAX_LIMIT + 100))
  out = apply_limit(text, limit=MAX_LIMIT + 5_000, keep='tail')
  # tail-keep puts the note on the before marker
  assert 'skipped before' in out
  assert f'limit {MAX_LIMIT + 5_000:,} clamped to {MAX_LIMIT:,}' in out


def test_clamp_below_one_clamps_to_one():
  out, note = _clamp(0)
  assert out == 1
  assert 'clamped to 1' in note


def test_clamp_at_or_under_max_passes_through():
  out, note = _clamp(MAX_LIMIT)
  assert out == MAX_LIMIT
  assert note == ''


def test_marker_omits_zero_segments():
  # only lines
  assert _marker('after', 5, 0) == '[...skipped after: 5 lines...]'
  # only bytes
  assert _marker('after', 0, 1024) == '[...skipped after: 1.0 KB...]'
  # both
  assert _marker('after', 5, 1024) == '[...skipped after: 5 lines / 1.0 KB...]'


def test_marker_includes_note():
  assert ' — limit 9 clamped to 8' in _marker('after', 3, 0, note='limit 9 clamped to 8')


def test_marker_collapses_to_note_when_nothing_skipped():
  # clamp note fires even when nothing was actually dropped — bare "skipped X: 0"
  # would read broken, so the marker collapses to just the note.
  assert _marker('after', 0, 0, note='limit 9 clamped to 8') == '[...limit 9 clamped to 8...]'


# ─── take_head ───────────────────────────────────────────────────────────────


def test_take_head_returns_whole_content_within_budget():
  kept, clamp_note = take_head('a\nb\nc\n', limit=10)
  assert kept == 'a\nb\nc\n'
  assert clamp_note == ''


def test_take_head_keeps_whole_lines_up_to_limit():
  content = ''.join(f'L{i}\n' for i in range(10))
  kept, _ = take_head(content, limit=3)
  assert kept == 'L0\nL1\nL2\n'
  # the caller paginates: the remainder is exactly what a cursor advance exposes next
  assert content[len(kept) :].startswith('L3\n')


def test_take_head_cuts_giant_first_line_mid_line():
  content = 'x' * (BYTE_LIMIT + 500) + '\n'
  kept, _ = take_head(content, limit=1)
  assert kept == 'x' * BYTE_LIMIT
  # successive calls over the remainder still make progress
  rest, _ = take_head(content[len(kept) :], limit=1)
  assert rest == 'x' * 500 + '\n'


def test_take_head_clamps_and_reports():
  kept, clamp_note = take_head('a\n', limit=MAX_LIMIT + 5)
  assert kept == 'a\n'
  assert clamp_note == f'limit {MAX_LIMIT + 5:,} clamped to {MAX_LIMIT:,}'


def test_take_head_empty_content():
  assert take_head('', limit=10) == ('', '')


# ─── window / numbered_window ────────────────────────────────────────────────


def test_window_returns_plain_lines_with_offset_markers():
  out = window('one\ntwo\nthree\nfour\n', offset=1, limit=2)
  assert (
    out == '[...skipped before: 1 lines / 4 B...]\ntwo\nthree\n[...skipped after: 1 lines / 5 B...]'
  )
  assert '\t' not in out


def test_window_offset_beyond_end_reports_whole_content_skipped():
  assert window('a\nb\n', offset=10) == '[...skipped before: 2 lines / 4 B...]'


def test_window_negative_offset_reads_from_start():
  assert window('a\nb\n', offset=-2) == 'a\nb'


def test_numbered_window_numbers_lines_one_based():
  out = numbered_window('aaa\nbbb\nccc\n')
  assert '    1\taaa' in out
  assert '    2\tbbb' in out
  assert '    3\tccc' in out
  assert 'skipped' not in out


def test_numbered_window_handles_missing_trailing_newline():
  out = numbered_window('aaa\nbbb')
  assert '    2\tbbb' in out


def test_numbered_window_offset_skips_and_keeps_absolute_numbers():
  out = numbered_window('1\n2\n3\n4\n5\n', offset=2, limit=2)
  assert '    1\t' not in out
  assert '    3\t3' in out
  assert '    4\t4' in out
  assert '    5\t' not in out
  assert 'skipped before: 2 lines' in out
  assert 'skipped after: 1 lines' in out


def test_numbered_window_offset_beyond_end_reports_whole_content_skipped():
  out = numbered_window('a\nb\n', offset=10)
  assert 'skipped before: 2 lines' in out
  assert '\t' not in out


def test_numbered_window_negative_offset_reads_from_start():
  out = numbered_window('a\nb\n', offset=-3)
  assert '    1\ta' in out
  assert 'skipped' not in out


def test_numbered_window_limit_caps_and_reports_after():
  content = ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50))
  out = numbered_window(content)
  assert 'skipped before' not in out
  assert 'skipped after: 50 lines' in out
  assert '    1\tline 0' in out
  assert f'{DEFAULT_LIMIT:>5}\tline {DEFAULT_LIMIT - 1}' in out
  assert f'{DEFAULT_LIMIT + 1:>5}\t' not in out


def test_numbered_window_clamps_oversized_limit():
  content = ''.join(f'L{i}\n' for i in range(5))
  out = numbered_window(content, limit=MAX_LIMIT + 1)
  assert f'limit {MAX_LIMIT + 1:,} clamped to {MAX_LIMIT:,}' in out


def test_numbered_window_empty_content_returns_empty():
  assert numbered_window('') == ''
