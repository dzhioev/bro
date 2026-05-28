import os
import tempfile

import pytest

from dev.mcp import (
  DEFAULT_LIMIT,
  MAX_LIMIT,
  MCPServer,
  _apply_limit,
  _clamp,
  _marker,
  bash,
  edit_file,
  glob,
  grep,
  read_file,
  read_reference,
  write_file,
)


def test_read_file_returns_numbered_lines():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    out = read_file(path)
    assert '    1\taaa' in out
    assert '    2\tbbb' in out
    assert '    3\tccc' in out
    # under cap → no markers
    assert 'skipped' not in out


def test_read_file_offset_emits_before_marker():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, '1\n2\n3\n4\n5\n')
    out = read_file(path, offset=2, limit=2)
    assert '    1\t' not in out
    assert '    3\t3' in out
    assert '    4\t4' in out
    assert '    5\t' not in out
    assert 'skipped before: 2 lines' in out
    assert 'skipped after: 1 lines' in out


def test_read_file_default_limit_caps_long_file():
  # produce DEFAULT_LIMIT + 50 lines; default should keep DEFAULT_LIMIT and
  # surface a `[...skipped after...]` marker noting 50 dropped lines.
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)))
    out = read_file(path)
    assert 'skipped before' not in out
    assert 'skipped after: 50 lines' in out
    assert '    1\tline 0' in out
    assert f'{DEFAULT_LIMIT:>5}\tline {DEFAULT_LIMIT - 1}' in out
    assert f'{DEFAULT_LIMIT + 1:>5}\t' not in out


def test_read_file_explicit_limit_returns_more():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)))
    out = read_file(path, limit=DEFAULT_LIMIT + 50)
    assert 'skipped' not in out
    assert f'{DEFAULT_LIMIT + 50:>5}\tline {DEFAULT_LIMIT + 49}' in out


def test_write_file_overwrites_and_creates_parents():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'a', 'b', 'c.txt')
    write_file(path, 'hi')
    assert open(path).read() == 'hi'
    write_file(path, 'bye')
    assert open(path).read() == 'bye'


def test_edit_file_unique_match():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    edit_file(path, 'bbb', 'BBB')
    assert open(path).read() == 'aaa\nBBB\nccc\n'


def test_edit_file_multi_match_without_replace_all_raises():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\naaa\n')
    with pytest.raises(ValueError, match='occurs 2 times'):
      edit_file(path, 'aaa', 'X')
    assert open(path).read() == 'aaa\naaa\n'


def test_edit_file_replace_all_replaces_every_occurrence():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\naaa\n')
    edit_file(path, 'aaa', 'X', replace_all=True)
    assert open(path).read() == 'X\nX\n'


def test_edit_file_not_found_raises():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\n')
    with pytest.raises(ValueError, match='old_string not found'):
      edit_file(path, 'zzz', 'X')


def test_bash_captures_stdout_and_exit_code():
  result = bash('echo hello')
  assert 'exit_code: 0' in result
  assert 'hello' in result
  assert 'skipped' not in result


def test_bash_captures_stderr():
  result = bash('echo oops 1>&2 ; false')
  assert 'exit_code: 1' in result
  assert 'oops' in result


def test_bash_timeout_returns_clearly():
  result = bash('sleep 5', timeout_seconds=1)
  assert 'TIMED OUT' in result


def test_bash_long_output_emits_before_marker_keeps_tail():
  # bash tails are usually most informative — confirm we keep the LAST `limit`
  # lines and report the dropped head via a [...skipped before...] marker.
  result = bash(f'for i in $(seq 1 {DEFAULT_LIMIT + 30}); do echo "L$i"; done')
  assert 'exit_code: 0' in result
  assert 'skipped before:' in result
  assert '30 lines' in result
  # last lines kept
  assert f'L{DEFAULT_LIMIT + 30}' in result
  # earliest lines dropped
  assert 'L1\n' not in result


def test_grep_finds_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'hello world\ngoodbye world\n')
    result = grep('hello', path=d)
    assert 'hello world' in result
    assert 'skipped' not in result


def test_grep_no_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'nothing here\n')
    assert grep('xyzzy', path=d) == 'no matches'


def test_grep_case_insensitive():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'HELLO\n')
    assert 'HELLO' in grep('hello', path=d, case_insensitive=True)
    assert grep('hello', path=d) == 'no matches'


def test_grep_glob_filter():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), 'target\n')
    write_file(os.path.join(d, 'a.txt'), 'target\n')
    result = grep('target', path=d, glob='*.py')
    assert 'a.py' in result
    assert 'a.txt' not in result


def test_grep_explicit_limit_truncates_and_emits_after_marker():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'x\n' * 10)
    result = grep('x', path=d, limit=3)
    assert result.count('\n') == 3  # 3 kept lines + after marker (no trailing \n)
    assert 'skipped after: 7 lines' in result


def test_grep_default_limit_caps_pathological_output():
  # without an explicit limit the old grep returned everything — a runaway result
  # crashed do.do. now we get DEFAULT_LIMIT lines + a marker reporting the rest.
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'match\n' * (DEFAULT_LIMIT * 3))
    result = grep('match', path=d)
    assert 'skipped after:' in result
    assert f'{DEFAULT_LIMIT * 2:,} lines' in result


def test_glob_returns_matches_sorted_by_mtime():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'old.py'), '')
    import time

    time.sleep(0.01)
    write_file(os.path.join(d, 'new.py'), '')
    result = glob('*.py', path=d).splitlines()
    assert result[0].endswith('new.py')
    assert result[1].endswith('old.py')


def test_glob_excludes_non_matches():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), '')
    write_file(os.path.join(d, 'b.txt'), '')
    result = glob('*.py', path=d)
    assert 'a.py' in result
    assert 'b.txt' not in result


def test_glob_no_matches():
  with tempfile.TemporaryDirectory() as d:
    assert glob('*.nonexistent', path=d) == 'no matches'


def test_mcpserver_no_args_lists_all_tools():
  import asyncio

  server = MCPServer()
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {
    'read_reference',
    'read_file',
    'write_file',
    'edit_file',
    'bash',
    'grep',
    'glob',
  }


def test_read_reference_returns_file_contents():
  ref = read_reference()
  # canary headings — keeps the test honest if REFERENCE.md is reorganised
  assert '# dev tools reference' in ref
  assert 'Output cap (`limit`)' in ref
  assert 'Skipped-content markers' in ref
  assert 'Fat-finger clamp' in ref


def test_mcpserver_subset_filters_tools():
  import asyncio

  server = MCPServer('read_file', 'bash')
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {'read_file', 'bash'}


def test_mcpserver_unknown_tool_raises():
  with pytest.raises(ValueError, match='unknown dev tools'):
    MCPServer('nope')


# ─── _apply_limit / _marker / _clamp ─────────────────────────────────────────


def test_apply_limit_passes_small_content_unchanged():
  text = 'a\nb\nc\n'
  out = _apply_limit(text, limit=DEFAULT_LIMIT, keep='head')
  assert 'skipped' not in out
  assert out.replace('\n', '') == 'abc'


def test_apply_limit_head_truncation_reports_after_only():
  text = ''.join(f'L{i}\n' for i in range(50))
  out = _apply_limit(text, limit=10, keep='head')
  assert out.startswith('L0\nL1')
  assert 'L9' in out
  assert 'L10' not in out
  assert 'skipped before' not in out
  assert 'skipped after: 40 lines' in out


def test_apply_limit_tail_truncation_reports_before_only():
  text = ''.join(f'L{i}\n' for i in range(50))
  out = _apply_limit(text, limit=10, keep='tail')
  assert 'skipped before: 40 lines' in out
  assert 'skipped after' not in out
  assert 'L49' in out
  assert 'L40' in out
  assert 'L39' not in out


def test_apply_limit_skipped_before_param_carries_into_marker():
  text = ''.join(f'L{i}\n' for i in range(5))
  out = _apply_limit(text, limit=10, keep='head', skipped_before_lines=42, skipped_before_bytes=900)
  # before marker reports the upstream offset, even though we kept everything we got
  assert 'skipped before: 42 lines / 900 B' in out
  assert 'skipped after' not in out


def test_apply_limit_byte_budget_fires_on_one_huge_line():
  # single line longer than the byte budget: lines-cap is fine but bytes are not.
  huge = 'x' * (DEFAULT_LIMIT * 150 + 5_000) + '\n'
  out = _apply_limit(huge, limit=DEFAULT_LIMIT, keep='head')
  # the giant line exceeds byte budget — kept zero lines, after marker reports it.
  assert 'skipped after: 1 lines' in out


def test_apply_limit_clamp_note_in_after_marker():
  text = ''.join(f'L{i}\n' for i in range(MAX_LIMIT + 100))
  out = _apply_limit(text, limit=MAX_LIMIT + 5_000, keep='head')
  assert f'limit {MAX_LIMIT + 5_000:,} clamped to {MAX_LIMIT:,}' in out


def test_apply_limit_clamp_note_in_before_marker_for_tail_keep():
  text = ''.join(f'L{i}\n' for i in range(MAX_LIMIT + 100))
  out = _apply_limit(text, limit=MAX_LIMIT + 5_000, keep='tail')
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
