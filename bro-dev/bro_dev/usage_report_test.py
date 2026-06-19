#!/usr/bin/env python
from usage_report import _format_table, _is_legacy_footer, _parse_footer

FOOTER_SINGLE = (
  "> created with Claude Code 2.1.181 | Opus 4.8: 45'231\n"
  '> session(s): 04ee83b5-ff91-4740-8791-073d14939b91'
)
FOOTER_MULTI = (
  "> created with Claude Code 2.1.114, 2.1.120 | Opus 4.8: 168'892, Haiku 4.5: 5'000\n"
  '> session(s): sid-a, sid-b'
)
FOOTER_RAW_SLUG = (
  '> created with Claude Code 2.1.181 | claude-experimental-99-12: 500\n> session(s): x'
)
LEGACY_FOOTER = (
  '> created with Claude Code 2.1.114 '
  '(Opus 4.7: 275,432; session: abc12345-1234-5678-9abc-def012345678)'
)
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
    assert out.session_ids == ['04ee83b5-ff91-4740-8791-073d14939b91']
    assert out.per_model == {'Opus 4.8': 45231}

  def test_multi_model_and_sessions(self):
    out = _parse_footer(FOOTER_MULTI)
    assert out is not None
    assert out.session_ids == ['sid-a', 'sid-b']
    assert out.per_model == {'Opus 4.8': 168892, 'Haiku 4.5': 5000}

  def test_raw_slug_label(self):
    out = _parse_footer(FOOTER_RAW_SLUG)
    assert out is not None
    assert out.per_model == {'claude-experimental-99-12': 500}

  def test_finds_footer_among_other_lines(self):
    out = _parse_footer(COMMIT_WITH_FOOTER)
    assert out is not None
    assert out.session_ids == ['04ee83b5-ff91-4740-8791-073d14939b91']

  def test_legacy_footer_is_not_parsed(self):
    assert _parse_footer(LEGACY_FOOTER) is None

  def test_no_footer_returns_none(self):
    assert _parse_footer(COMMIT_NO_FOOTER) is None

  def test_empty_string_returns_none(self):
    assert _parse_footer('') is None


class TestIsLegacyFooter:
  def test_legacy_detected(self):
    assert _is_legacy_footer(LEGACY_FOOTER) is True

  def test_new_format_not_legacy(self):
    assert _is_legacy_footer(FOOTER_SINGLE) is False
    assert _is_legacy_footer(FOOTER_MULTI) is False

  def test_footerless_not_legacy(self):
    assert _is_legacy_footer(COMMIT_NO_FOOTER) is False


class TestFormatTable:
  def test_renders_per_model_and_total(self):
    out = _format_table({'Opus 4.8': 1_000_000, 'Sonnet 4.6': 500_000}, 12, 8, 0)
    assert 'commits scanned: 12' in out
    assert 'delta footers summed: 8' in out
    assert 'legacy footers skipped: 0' in out
    assert '1,000,000' in out
    assert '500,000' in out
    assert '1,500,000' in out

  def test_reports_legacy_count(self):
    out = _format_table({'Opus 4.8': 10}, 5, 2, 3)
    assert 'delta footers summed: 2' in out
    assert 'legacy footers skipped: 3' in out

  def test_empty_totals_still_prints_grand_total_zero(self):
    out = _format_table({}, 3, 0, 0)
    assert 'commits scanned: 3' in out
    assert 'delta footers summed: 0' in out
    # grand total line still present
    assert '0' in out
