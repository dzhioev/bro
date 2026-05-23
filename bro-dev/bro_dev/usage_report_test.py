#!/usr/bin/env python
from usage_report import _parse_footer, _format_table


FOOTER_SINGLE = (
  '> created with Claude Code 2.1.114 '
  '(Opus 4.7: 275,432; session: abc12345-1234-5678-9abc-def012345678)'
)
FOOTER_MULTI = (
  '> created with Claude Code 2.1.114 (Opus 4.7: 1,275,432, Sonnet 4.6: 12,345; session: abc-456)'
)
FOOTER_RAW_SLUG = '> created with Claude Code 2.1.114 (claude-experimental-99-12: 500; session: x)'
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
    assert out.session_id == 'abc12345-1234-5678-9abc-def012345678'
    assert out.per_model == {'Opus 4.7': 275432}

  def test_multi_model(self):
    out = _parse_footer(FOOTER_MULTI)
    assert out is not None
    assert out.session_id == 'abc-456'
    assert out.per_model == {'Opus 4.7': 1275432, 'Sonnet 4.6': 12345}

  def test_raw_slug_label(self):
    out = _parse_footer(FOOTER_RAW_SLUG)
    assert out is not None
    assert out.per_model == {'claude-experimental-99-12': 500}

  def test_finds_footer_among_other_lines(self):
    out = _parse_footer(COMMIT_WITH_FOOTER)
    assert out is not None
    assert out.session_id == 'abc12345-1234-5678-9abc-def012345678'

  def test_no_footer_returns_none(self):
    assert _parse_footer(COMMIT_NO_FOOTER) is None

  def test_empty_string_returns_none(self):
    assert _parse_footer('') is None


class TestFormatTable:
  def test_renders_per_model_and_total(self):
    out = _format_table({'Opus 4.7': 1_000_000, 'Sonnet 4.6': 500_000}, 12, 8)
    assert 'commits scanned: 12' in out
    assert 'commits with footer: 8' in out
    assert '1,000,000' in out
    assert '500,000' in out
    assert '1,500,000' in out

  def test_empty_totals_still_prints_grand_total_zero(self):
    out = _format_table({}, 3, 0)
    assert 'commits scanned: 3' in out
    assert 'commits with footer: 0' in out
    # grand total line still present
    assert '0' in out
