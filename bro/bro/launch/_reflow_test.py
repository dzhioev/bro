"""tests for do/_reflow.py — display→logical alignment and selection extraction."""

from typing import Optional

from do._reflow import LineSpan, Reflow


def select_all(y: int) -> Optional[tuple[int, int]]:
  return 0, -1


def test_wrapped_line_rejoins_and_padding_drops():
  reflow = Reflow(
    ['aaa bbb                ', 'ccc ddd    '],
    ['aaa bbb ccc ddd'],
  )
  assert reflow.extract(select_all) == 'aaa bbb ccc ddd'


def test_explicit_break_survives():
  reflow = Reflow(['one   ', '', 'two  '], ['one', '', 'two'])
  assert reflow.extract(select_all) == 'one\n\ntwo'


def test_wrap_consumed_whitespace_is_restored_verbatim():
  # the wrap ate two spaces at the fold; the joined copy carries both
  reflow = Reflow(['cmd  --flag      ', 'value  '], ['cmd  --flag  value'])
  assert reflow.extract(select_all) == 'cmd  --flag  value'


def test_code_indentation_is_kept():
  reflow = Reflow(
    ['def f():         ', '    return 1     '],
    ['def f():', '    return 1'],
  )
  assert reflow.line_spans == [LineSpan(0, 0, 0, 8), LineSpan(1, 0, 0, 12)]
  assert reflow.extract(select_all) == 'def f():\n    return 1'


def test_prefixed_continuation_maps_into_the_logical_line():
  # a bullet list renders a marker on the first line and plain indent after it
  reflow = Reflow(
    [' • first bullet   ', '   continues here  ', ' • second  '],
    [' • first bullet continues here', ' • second'],
  )
  assert reflow.extract(select_all) == ' • first bullet continues here\n • second'


def test_partial_selection_across_a_fold_joins_without_a_break():
  reflow = Reflow(['aaa bbb   ', 'ccc ddd'], ['aaa bbb ccc ddd'])
  spans = {0: (4, -1), 1: (0, 3)}
  assert reflow.extract(spans.get) == 'bbb ccc'


def test_selection_in_trailing_padding_clamps_to_content():
  reflow = Reflow(['short          '], ['short'])
  spans = {0: (2, 12)}
  assert reflow.extract(spans.get) == 'ort'


def test_unmatched_lines_degrade_to_display_text_and_resync_at_blank():
  reflow = Reflow(
    ['╭──╮   ', '│xy│   ', '', 'tail  '],
    ['╭────────╮', '│xy      │', '', 'tail'],
  )
  assert reflow.line_spans[0] is None
  assert reflow.line_spans[1] is None
  assert reflow.line_spans[2] is not None
  assert reflow.line_spans[3] is not None
  assert reflow.extract(select_all) == '╭──╮\n│xy│\n\ntail'


def test_blank_runs_collapse_and_edges_trim():
  reflow = Reflow(
    ['', 'one  ', '', '', 'two ', ''],
    ['', 'one', '', '', 'two', ''],
  )
  assert reflow.extract(select_all) == 'one\n\ntwo'


def test_unselected_lines_split_groups():
  reflow = Reflow(['aaa bbb  ', 'ccc ddd '], ['aaa bbb ccc ddd'])
  spans = {1: (0, -1)}
  assert reflow.extract(spans.get) == 'ccc ddd'
