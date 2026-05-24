#!/usr/bin/env python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from git_golc import _format_credits, _model_initial, _parse_footer, round_credits  # noqa: E402

FOOTER_SINGLE = (
  '> created with Claude Code 2.1.114 '
  '(Opus 4.7: 275,432; session: abc12345-1234-5678-9abc-def012345678)'
)
FOOTER_MULTI = (
  '> created with Claude Code 2.1.114 (Opus 4.7: 1,275,432, Sonnet 4.6: 12,345; session: x)'
)
COMMIT_NO_FOOTER = """chore: bump deps

routine update.
"""


class TestRoundCredits:
  def test_under_thousand_exact(self):
    assert round_credits(0) == '0'
    assert round_credits(1) == '1'
    assert round_credits(500) == '500'
    assert round_credits(999) == '999'

  def test_k_one_decimal_when_under_five(self):
    assert round_credits(1_234) == '1.2K'
    assert round_credits(4_999) == '5.0K'

  def test_k_integer_when_at_least_five(self):
    assert round_credits(5_000) == '5K'
    assert round_credits(18_432) == '18K'
    assert round_credits(500_000) == '500K'

  def test_m_one_decimal_when_under_five(self):
    assert round_credits(1_234_567) == '1.2M'
    assert round_credits(4_900_000) == '4.9M'

  def test_m_integer_when_at_least_five(self):
    assert round_credits(12_000_000) == '12M'
    assert round_credits(500_000_000) == '500M'

  def test_promote_to_next_unit_when_rounding_pushes_over(self):
    assert round_credits(999_500) == '1.0M'
    assert round_credits(999_999_500) == '1.0B'

  def test_billion(self):
    assert round_credits(1_234_000_000) == '1.2B'
    assert round_credits(12_000_000_000) == '12B'


class TestParseFooter:
  def test_single_model(self):
    assert _parse_footer(FOOTER_SINGLE) == {'Opus 4.7': 275432}

  def test_multi_model(self):
    assert _parse_footer(FOOTER_MULTI) == {'Opus 4.7': 1275432, 'Sonnet 4.6': 12345}

  def test_no_footer(self):
    assert _parse_footer(COMMIT_NO_FOOTER) is None

  def test_empty_string(self):
    assert _parse_footer('') is None


class TestModelInitial:
  def test_first_letter_uppercased(self):
    assert _model_initial('Opus 4.7') == 'O'
    assert _model_initial('Sonnet 4.6') == 'S'
    assert _model_initial('Haiku 4.5') == 'H'

  def test_raw_slug(self):
    assert _model_initial('claude-experimental-99-12') == 'C'

  def test_empty_label(self):
    assert _model_initial('') == '?'


class TestFormatCredits:
  def test_single_model(self):
    assert _format_credits({'Opus 4.7': 18_432}) == 'O:18K'

  def test_multi_model_sorted_alphabetically_by_label(self):
    # 'Haiku' < 'Opus' < 'Sonnet' alphabetically
    out = _format_credits({'Opus 4.7': 1_200_000, 'Sonnet 4.6': 50_000, 'Haiku 4.5': 2_000})
    assert out == 'H:2.0K O:1.2M S:50K'
