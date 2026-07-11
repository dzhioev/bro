#!/usr/bin/env python
from usage_report import _format_table


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


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
