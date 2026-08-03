#!/usr/bin/env python
from bro_dev.git_golc import (
  _SENTINEL_RE,
  _format_credits,
  _model_initial,
  _parse_footer,
  round_credits,
)

FOOTER_SINGLE = "> created with Claude Code 2.1.181 | Opus 4.8: ↑(1 2 3) ↓275'432"
FOOTER_MULTI = (
  '> created with Claude Code 2.1.114, 2.1.120 | '
  "Opus 4.8: ↑(0 0 0) ↓1'275'432, Sonnet 4.6: ↑(0 0 0) ↓12'345"
)
FOOTER_BRO = '> created with bro//ppp-dev | gpt-5: ↑(70 0 30) ↓22'
# an old single-number footer (pre four-class redesign) — must no longer parse
OLD_SINGLE = "> created with Claude Code 2.1.114 | Opus 4.8: 275'432\n> session(s): abc12345"
# the previous four-class shape (`↑ a / b (c) ↓ d`) — must no longer parse
PREVIOUS_FOOTER = "> created with Claude Code 2.1.181 | Opus 4.8: ↑ 1 / 2 (3) ↓ 275'432"
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
  def test_single_model_output(self):
    assert _parse_footer(FOOTER_SINGLE) == {'Opus 4.8': 275432}

  def test_multi_model_output(self):
    assert _parse_footer(FOOTER_MULTI) == {'Opus 4.8': 1275432, 'Sonnet 4.6': 12345}

  def test_bro_agent_footer(self):
    assert _parse_footer(FOOTER_BRO) == {'gpt-5': 22}

  def test_old_single_number_footer_is_not_parsed(self):
    assert _parse_footer(OLD_SINGLE) is None

  def test_previous_slash_parens_footer_is_not_parsed(self):
    assert _parse_footer(PREVIOUS_FOOTER) is None

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


class TestSentinelRegex:
  SHA = 'a' * 40

  def test_plain(self):
    m = _SENTINEL_RE.search(f'CREDITS:{self.SHA} subject')
    assert m is not None and m.group(1) == self.SHA

  def test_ansi_wrapped_sha(self):
    # `%H` under `%C(auto)` arrives wrapped in yellow + reset
    s = f'CREDITS:\x1b[33m{self.SHA}\x1b[m\x1b[33m (HEAD)\x1b[m subject'
    m = _SENTINEL_RE.search(s)
    assert m is not None and m.group(1) == self.SHA
    # the trailing decoration color must survive substitution
    assert _SENTINEL_RE.sub('CR', s) == 'CR\x1b[33m (HEAD)\x1b[m subject'
