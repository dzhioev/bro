#!/usr/bin/env python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from claude_commit_footer import (  # noqa: E402
  Footer,
  State,
  _cumulative_usage,
  _emit_default,
  _emit_squash,
  _fmt_int,
  _format_footer,
  _model_label,
  _parse_footer,
  _to_labels,
  _version,
)

OPUS = 'claude-opus-4-8'
HAIKU = 'claude-haiku-4-5-20251001'

LEGACY_FOOTER = (
  '> created with Claude Code 2.1.114 '
  '(Opus 4.7: 275,432; session: abc12345-1234-5678-9abc-def012345678)'
)


class TestFmtInt:
  def test_apostrophe_thousands(self):
    assert _fmt_int(0) == '0'
    assert _fmt_int(5_000) == "5'000"
    assert _fmt_int(45_231) == "45'231"
    assert _fmt_int(168_892) == "168'892"
    assert _fmt_int(1_275_432) == "1'275'432"


class TestModelLabel:
  def test_known_families(self):
    assert _model_label(OPUS) == 'Opus 4.8'
    assert _model_label(HAIKU) == 'Haiku 4.5'
    assert _model_label('claude-sonnet-4-6') == 'Sonnet 4.6'

  def test_unknown_slug_passes_through(self):
    assert _model_label('<synthetic>') == '<synthetic>'
    assert _model_label('claude-experimental-99-12') == 'claude-experimental-99-12'


class TestCumulativeUsage:
  def _write(self, path, rows):
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

  def _msg(self, model, output):
    return {'message': {'model': model, 'usage': {'output_tokens': output}}}

  def test_sums_per_model(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg(OPUS, 10), self._msg(OPUS, 5), self._msg(HAIKU, 3)])
    assert _cumulative_usage(p) == {OPUS: 15, HAIKU: 3}

  def test_skips_synthetic(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg(OPUS, 10), self._msg('<synthetic>', 999)])
    assert _cumulative_usage(p) == {OPUS: 10}

  def test_all_synthetic_yields_empty(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg('<synthetic>', 12), self._msg('<synthetic>', 7)])
    assert _cumulative_usage(p) == {}


class TestToLabels:
  def test_collapses_slugs_to_labels(self):
    assert _to_labels({OPUS: 100, HAIKU: 5}) == {'Opus 4.8': 100, 'Haiku 4.5': 5}

  def test_same_label_different_date_merges(self):
    out = _to_labels({'claude-haiku-4-5-20251001': 5, 'claude-haiku-4-5-20260101': 7})
    assert out == {'Haiku 4.5': 12}


class TestVersion:
  def test_from_ai_agent(self, monkeypatch):
    monkeypatch.setenv('AI_AGENT', 'claude-code_2-1-181_agent')
    assert _version() == '2.1.181'

  def test_from_versioned_execpath(self, monkeypatch):
    monkeypatch.delenv('AI_AGENT', raising=False)
    monkeypatch.setenv('CLAUDE_CODE_EXECPATH', '/home/u/.local/versions/2.1.181/claude')
    assert _version() == '2.1.181'

  def test_non_version_execpath_falls_back(self, monkeypatch):
    monkeypatch.delenv('AI_AGENT', raising=False)
    monkeypatch.setenv('CLAUDE_CODE_EXECPATH', '/usr/lib/node_modules/.../bin/claude.exe')
    assert _version() == 'unknown'


class TestFormatFooter:
  def test_single_model_single_session(self):
    out = _format_footer(['2.1.114'], {'Opus 4.8': 45_231}, ['04ee83b5'])
    assert out == ("> created with Claude Code 2.1.114 | Opus 4.8: 45'231\n> session(s): 04ee83b5")

  def test_multi_everything(self):
    out = _format_footer(
      ['2.1.114', '2.1.120'],
      {'Opus 4.8': 168_892, 'Haiku 4.5': 5_000},
      ['04ee83b5', '9a2c1f00'],
    )
    assert out == (
      "> created with Claude Code 2.1.114, 2.1.120 | Opus 4.8: 168'892, Haiku 4.5: 5'000\n"
      '> session(s): 04ee83b5, 9a2c1f00'
    )


class TestParseFooter:
  def test_round_trips_format_footer(self):
    versions = ['2.1.114', '2.1.120']
    tokens = {'Opus 4.8': 168_892, 'Haiku 4.5': 5_000}
    sessions = ['04ee83b5', '9a2c1f00']
    parsed = _parse_footer(_format_footer(versions, tokens, sessions))
    assert parsed == Footer(versions=versions, delta=tokens, sessions=sessions)

  def test_single(self):
    parsed = _parse_footer(
      "> created with Claude Code 2.1.114 | Opus 4.8: 45'231\n> session(s): sid-1"
    )
    assert parsed == Footer(versions=['2.1.114'], delta={'Opus 4.8': 45231}, sessions=['sid-1'])

  def test_finds_footer_among_other_lines(self):
    msg = f'fix: a thing\n\nbody text\n\n{_format_footer(["2.1"], {"Opus 4.8": 10}, ["s"])}\n'
    parsed = _parse_footer(msg)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': 10}

  def test_legacy_footer_does_not_match(self):
    assert _parse_footer(LEGACY_FOOTER) is None

  def test_footerless(self):
    assert _parse_footer('chore: bump deps\n\nroutine.\n') is None
    assert _parse_footer('') is None


class TestState:
  def test_missing_file_is_empty(self, tmp_path):
    s = State(tmp_path / 'state.json')
    assert s.baseline('whoever') == {}

  def test_stage_then_record_promotes(self, tmp_path):
    p = tmp_path / 'state.json'
    s = State(p)
    s.stage('S', {OPUS: 100})
    # staged, not yet committed
    assert State(p).baseline('S') == {}
    s.record()
    assert State(p).baseline('S') == {OPUS: 100}
    # staged cleared after record
    assert State(p).staged == {}

  def test_record_with_nothing_staged_is_noop(self, tmp_path):
    p = tmp_path / 'state.json'
    State(p).record()
    assert State(p).committed == {}

  def test_corrupt_file_falls_back_to_empty(self, tmp_path):
    p = tmp_path / 'state.json'
    p.write_text('{ not json')
    assert State(p).baseline('S') == {}


class TestEmitDefault:
  def test_first_commit_takes_full_cumulative(self, tmp_path):
    s = State(tmp_path / 'state.json')
    footer = _emit_default({OPUS: 100}, 'S', '2.1', s)
    assert 'Opus 4.8: 100' in footer
    assert '> session(s): S' in footer
    # cum_now staged for promotion
    assert s.staged['S'] == {OPUS: 100}

  def test_second_commit_is_delta(self, tmp_path):
    p = tmp_path / 'state.json'
    s = State(p)
    _emit_default({OPUS: 100}, 'S', '2.1', s)
    s.record()
    footer = _emit_default({OPUS: 130}, 'S', '2.1', State(p))
    assert 'Opus 4.8: 30' in footer

  def test_deltas_telescope_to_final_cumulative(self, tmp_path):
    p = tmp_path / 'state.json'
    cums = [{OPUS: 100}, {OPUS: 130}, {OPUS: 175}]
    total = 0
    for cum in cums:
      s = State(p)
      parsed = _parse_footer(_emit_default(cum, 'S', '2.1', s))
      assert parsed is not None
      total += parsed.delta['Opus 4.8']
      s.record()
    assert total == 175  # == final cumulative


class TestEmitSquash:
  def _commit(self, version: str, tokens: dict[str, int], session: str) -> tuple[str, str]:
    return ('0' * 40, f'subject\n\n{_format_footer([version], tokens, [session])}\n')

  def test_auto_case_reduces_to_land_cumulative(self, tmp_path):
    # one session authored both branch commits and is also the land session;
    # branch deltas telescope to committed[L], remainder adds L's /land work.
    p = tmp_path / 'state.json'
    s = State(p)
    s.committed['L'] = {OPUS: 130}  # mark after L's last branch commit
    commits = [
      self._commit('2.1', {'Opus 4.8': 100}, 'L'),
      self._commit('2.1', {'Opus 4.8': 30}, 'L'),
    ]
    footer, footerless = _emit_squash(commits, ('L', {OPUS: 150}), '2.1', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': 150}  # 100 + 30 + (150 - 130)
    assert parsed.sessions == ['L']
    assert parsed.versions == ['2.1']
    assert footerless == []

  def test_unions_sessions_and_versions_sorted(self, tmp_path):
    s = State(tmp_path / 'state.json')  # land session authored no branch commits
    commits = [
      self._commit('2.1.120', {'Opus 4.8': 100}, 'A'),
      self._commit('2.1.114', {'Haiku 4.5': 50}, 'B'),
    ]
    footer, _ = _emit_squash(commits, ('L', {OPUS: 40}), '2.1.130', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': 140, 'Haiku 4.5': 50}
    assert parsed.sessions == ['A', 'B', 'L']
    assert parsed.versions == ['2.1.114', '2.1.120', '2.1.130']

  def test_footerless_commit_flagged_and_zero(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits = [
      self._commit('2.1', {'Opus 4.8': 100}, 'A'),
      ('abcdef1234' + '0' * 30, 'chore: no footer\n\nbody\n'),
    ]
    footer, footerless = _emit_squash(commits, None, '2.1', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': 100}
    assert footerless == ['abcdef1234' + '0' * 30]

  def test_no_land_session_aggregates_branch_only(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits = [self._commit('2.1', {'Opus 4.8': 100}, 'A')]
    footer, _ = _emit_squash(commits, None, '2.1', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.sessions == ['A']
    assert parsed.delta == {'Opus 4.8': 100}
