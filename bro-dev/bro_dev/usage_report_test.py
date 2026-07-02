#!/usr/bin/env python
from usage_report import _format_table, _parse_footer


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


FOOTER_SINGLE = (
  "> created with Claude Code 2.1.181 | Opus 4.8: ↑(48'787 2'103'810 41'676'292) ↓434'029"
)
FOOTER_MULTI = (
  '> created with Claude Code 2.1.114, 2.1.120 | Opus 4.8: ↑(1 2 3) ↓4, Haiku 4.5: ↑(5 6 7) ↓8'
)
FOOTER_RAW_SLUG = '> created with Claude Code 2.1.181 | claude-experimental-99-12: ↑(0 0 0) ↓500'
# an old single-number footer (pre four-class redesign) — must no longer parse
OLD_FOOTER = "> created with Claude Code 2.1.114 | Opus 4.8: 45'231\n> session(s): abc12345"
# the previous four-class shape (`↑ a / b (c) ↓ d`) — must no longer parse
PREV_FOOTER = "> created with Claude Code 2.1.181 | Opus 4.8: ↑ 4'812 / 18'903 (1'204'556) ↓ 12'905"
COMMIT_WITH_FOOTER = f"""fix(server): tighten input validation

ensure trailing slashes are normalised before lookup.

{FOOTER_SINGLE}
"""
COMMIT_NO_FOOTER = """chore: bump deps

routine update.
"""


class TestParseFooter:
  def test_single_model(self):
    out = _parse_footer(FOOTER_SINGLE)
    assert out is not None
    assert out.per_model == {
      'Opus 4.8': C(input=48_787, cache_write=2_103_810, cache_read=41_676_292, output=434_029)
    }

  def test_multi_model(self):
    out = _parse_footer(FOOTER_MULTI)
    assert out is not None
    assert out.per_model == {
      'Opus 4.8': C(input=1, cache_write=2, cache_read=3, output=4),
      'Haiku 4.5': C(input=5, cache_write=6, cache_read=7, output=8),
    }

  def test_raw_slug_label(self):
    out = _parse_footer(FOOTER_RAW_SLUG)
    assert out is not None
    assert out.per_model == {'claude-experimental-99-12': C(output=500)}

  def test_finds_footer_among_other_lines(self):
    out = _parse_footer(COMMIT_WITH_FOOTER)
    assert out is not None
    assert out.per_model['Opus 4.8']['output'] == 434_029

  def test_old_single_number_footer_is_not_parsed(self):
    assert _parse_footer(OLD_FOOTER) is None

  def test_prev_slash_parens_footer_is_not_parsed(self):
    assert _parse_footer(PREV_FOOTER) is None

  def test_no_footer_returns_none(self):
    assert _parse_footer(COMMIT_NO_FOOTER) is None

  def test_empty_string_returns_none(self):
    assert _parse_footer('') is None


class TestFormatTable:
  def test_renders_per_class_columns_and_total(self):
    totals = {
      'Opus 4.8': C(input=1_000, cache_write=2_000, cache_read=3_000, output=4_000),
      'Sonnet 4.6': C(input=10, cache_write=20, cache_read=30, output=40),
    }
    out = _format_table(totals, 12, 8)
    assert 'commits scanned: 12' in out
    assert 'footers summed: 8' in out
    # per-class column headers
    for header in ('input', 'cache-write', 'cache-read', 'output'):
      assert header in out
    # apostrophe thousands separator, and a summed total row
    assert "1'010" in out  # input total
    assert "4'040" in out  # output total
    assert 'total' in out

  def test_empty_totals_still_prints_grand_total(self):
    out = _format_table({}, 3, 0)
    assert 'commits scanned: 3' in out
    assert 'footers summed: 0' in out
    assert 'total' in out
