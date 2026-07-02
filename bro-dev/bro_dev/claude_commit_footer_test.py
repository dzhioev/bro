#!/usr/bin/env python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from claude_commit_footer import (  # noqa: E402
  Footer,
  State,
  _cumulative_usage,
  _effective_baseline,
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

# an old single-number footer (pre four-class redesign) — must no longer parse
OLD_FOOTER = "> created with Claude Code 2.1.114 | Opus 4.8: 45'231\n> session(s): abc12345"
# the previous four-class shape (`↑ a / b (c) ↓ d`) — must no longer parse
PREV_FOOTER = "> created with Claude Code 2.1.181 | Opus 4.8: ↑ 4'812 / 18'903 (1'204'556) ↓ 12'905"


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


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

  def test_single_number_families(self):
    assert _model_label('claude-fable-5') == 'Fable 5'
    assert _model_label('claude-mythos-5') == 'Mythos 5'

  def test_unknown_slug_passes_through(self):
    assert _model_label('<synthetic>') == '<synthetic>'
    assert _model_label('claude-experimental-99-12') == 'claude-experimental-99-12'


class TestCumulativeUsage:
  def _write(self, path, rows):
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

  def _msg(self, model, input=0, cache_write=0, cache_read=0, output=0):
    return {
      'message': {
        'model': model,
        'usage': {
          'input_tokens': input,
          'cache_creation_input_tokens': cache_write,
          'cache_read_input_tokens': cache_read,
          'output_tokens': output,
        },
      }
    }

  def test_sums_per_model_per_class(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(
      p,
      [
        self._msg(OPUS, input=2, cache_write=3, cache_read=4, output=10),
        self._msg(OPUS, input=1, cache_write=1, cache_read=1, output=5),
        self._msg(HAIKU, output=3),
      ],
    )
    assert _cumulative_usage(p) == {
      OPUS: C(input=3, cache_write=4, cache_read=5, output=15),
      HAIKU: C(output=3),
    }

  def test_missing_fields_default_to_zero(self, tmp_path):
    p = tmp_path / 't.jsonl'
    # only output present in the usage block
    p.write_text(json.dumps({'message': {'model': OPUS, 'usage': {'output_tokens': 7}}}) + '\n')
    assert _cumulative_usage(p) == {OPUS: C(output=7)}

  def test_skips_synthetic(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg(OPUS, output=10), self._msg('<synthetic>', output=999)])
    assert _cumulative_usage(p) == {OPUS: C(output=10)}

  def test_all_synthetic_yields_empty(self, tmp_path):
    p = tmp_path / 't.jsonl'
    self._write(p, [self._msg('<synthetic>', output=12), self._msg('<synthetic>', output=7)])
    assert _cumulative_usage(p) == {}


class TestToLabels:
  def test_collapses_slugs_to_labels(self):
    out = _to_labels({OPUS: C(output=100), HAIKU: C(output=5)})
    assert out == {'Opus 4.8': C(output=100), 'Haiku 4.5': C(output=5)}

  def test_same_label_different_date_merges_per_class(self):
    out = _to_labels(
      {
        'claude-haiku-4-5-20251001': C(input=1, output=5),
        'claude-haiku-4-5-20260101': C(input=2, output=7),
      }
    )
    assert out == {'Haiku 4.5': C(input=3, output=12)}


class TestEffectiveBaseline:
  def test_normal_growth_uses_committed(self):
    committed = C(input=10, output=20)
    assert _effective_baseline(committed, C(input=15, output=30)) == committed

  def test_equal_uses_committed(self):
    committed = C(output=20)
    assert _effective_baseline(committed, C(output=20)) == committed

  def test_any_class_backwards_resets_to_zero(self):
    # output dropped (a new transcript reused the worktree) -> reset even though
    # input grew; a within-session cumulative could never go backwards in any class
    assert _effective_baseline(C(input=10, output=20), C(input=15, output=5)) == C()


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
  def test_single_model(self):
    out = _format_footer(
      ['2.1.114'],
      {'Opus 4.8': C(input=48_787, cache_write=2_103_810, cache_read=41_676_292, output=434_029)},
    )
    assert out == (
      "> created with Claude Code 2.1.114 | Opus 4.8: ↑(48'787 2'103'810 41'676'292) ↓434'029"
    )

  def test_multi_model(self):
    out = _format_footer(
      ['2.1.114', '2.1.120'],
      {'Opus 4.8': C(input=168_892, output=10), 'Haiku 4.5': C(cache_read=5_000)},
    )
    assert out == (
      '> created with Claude Code 2.1.114, 2.1.120 | '
      "Opus 4.8: ↑(168'892 0 0) ↓10, Haiku 4.5: ↑(0 0 5'000) ↓0"
    )


class TestParseFooter:
  def test_round_trips_format_footer(self):
    versions = ['2.1.114', '2.1.120']
    tokens = {
      'Opus 4.8': C(input=1, cache_write=2, cache_read=3, output=4),
      'Haiku 4.5': C(output=5_000),
    }
    parsed = _parse_footer(_format_footer(versions, tokens))
    assert parsed == Footer(versions=versions, delta=tokens)

  def test_single(self):
    parsed = _parse_footer(
      "> created with Claude Code 2.1.114 | Opus 4.8: ↑(48'787 2'103'810 41'676'292) ↓434'029"
    )
    assert parsed == Footer(
      versions=['2.1.114'],
      delta={
        'Opus 4.8': C(input=48_787, cache_write=2_103_810, cache_read=41_676_292, output=434_029)
      },
    )

  def test_finds_footer_among_other_lines(self):
    msg = f'fix: a thing\n\nbody text\n\n{_format_footer(["2.1"], {"Opus 4.8": C(output=10)})}\n'
    parsed = _parse_footer(msg)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=10)}

  def test_old_single_number_footer_does_not_parse(self):
    assert _parse_footer(OLD_FOOTER) is None

  def test_prev_slash_parens_footer_does_not_parse(self):
    assert _parse_footer(PREV_FOOTER) is None

  def test_footerless(self):
    assert _parse_footer('chore: bump deps\n\nroutine.\n') is None
    assert _parse_footer('') is None


class TestState:
  def test_missing_file_is_empty(self, tmp_path):
    s = State(tmp_path / 'state.json')
    assert s.committed == {}
    assert s.staged == {}

  def test_stage_then_record_promotes(self, tmp_path):
    p = tmp_path / 'state.json'
    s = State(p)
    s.stage({OPUS: C(output=100)})
    # staged, not yet committed
    assert State(p).committed == {}
    s.record()
    assert State(p).committed == {OPUS: C(output=100)}
    # staged cleared after record
    assert State(p).staged == {}

  def test_record_with_nothing_staged_is_noop(self, tmp_path):
    p = tmp_path / 'state.json'
    State(p).record()
    assert State(p).committed == {}

  def test_corrupt_file_falls_back_to_empty(self, tmp_path):
    p = tmp_path / 'state.json'
    p.write_text('{ not json')
    assert State(p).committed == {}


class TestEmitDefault:
  def test_first_commit_takes_full_cumulative(self, tmp_path):
    s = State(tmp_path / 'state.json')
    footer = _emit_default({OPUS: C(output=100)}, '2.1', s)
    assert 'Opus 4.8: ↑(0 0 0) ↓100' in footer
    # cum_now staged for promotion
    assert s.staged[OPUS] == C(output=100)

  def test_second_commit_is_delta(self, tmp_path):
    p = tmp_path / 'state.json'
    s = State(p)
    _emit_default({OPUS: C(output=100)}, '2.1', s)
    s.record()
    footer = _emit_default({OPUS: C(output=130)}, '2.1', State(p))
    assert '↓30' in footer

  def test_transcript_reset_recredits_full_cumulative(self, tmp_path):
    s = State(tmp_path / 'state.json')
    s.committed[OPUS] = C(output=5_000)  # a prior session's mark in this worktree
    # a new session reuses the worktree: cumulative reset, smaller than the mark
    parsed = _parse_footer(_emit_default({OPUS: C(output=200)}, '2.1', s))
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=200)}  # full new cumulative, not negative

  def test_deltas_telescope_to_final_cumulative(self, tmp_path):
    p = tmp_path / 'state.json'
    cums = [C(output=100), C(output=130), C(output=175)]
    total = 0
    for cum in cums:
      s = State(p)
      parsed = _parse_footer(_emit_default({OPUS: cum}, '2.1', s))
      assert parsed is not None
      total += parsed.delta['Opus 4.8']['output']
      s.record()
    assert total == 175  # == final cumulative


class TestEmitSquash:
  def _commit(self, version, tokens) -> tuple[str, str]:
    return ('0' * 40, f'subject\n\n{_format_footer([version], tokens)}\n')

  def test_auto_case_reduces_to_land_cumulative(self, tmp_path):
    # one session authored both branch commits and is also the land session;
    # branch deltas telescope to committed, remainder adds the /land work.
    p = tmp_path / 'state.json'
    s = State(p)
    s.committed[OPUS] = C(output=130)  # mark after the last branch commit
    commits = [
      self._commit('2.1', {'Opus 4.8': C(output=100)}),
      self._commit('2.1', {'Opus 4.8': C(output=30)}),
    ]
    footer, footerless = _emit_squash(commits, {OPUS: C(output=150)}, '2.1', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=150)}  # 100 + 30 + (150 - 130)
    assert parsed.versions == ['2.1']
    assert footerless == []

  def test_unions_versions_sorted_and_sums_classes(self, tmp_path):
    s = State(tmp_path / 'state.json')  # land session authored no branch commits
    commits = [
      self._commit('2.1.120', {'Opus 4.8': C(input=100, output=1)}),
      self._commit('2.1.114', {'Haiku 4.5': C(cache_read=50)}),
    ]
    footer, _ = _emit_squash(commits, {OPUS: C(input=40)}, '2.1.130', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(input=140, output=1), 'Haiku 4.5': C(cache_read=50)}
    assert parsed.versions == ['2.1.114', '2.1.120', '2.1.130']

  def test_footerless_commit_flagged_and_zero(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits: list[tuple[str, str]] = [
      self._commit('2.1', {'Opus 4.8': C(output=100)}),
      ('abcdef1234' + '0' * 30, 'chore: no footer\n\nbody\n'),
    ]
    footer, footerless = _emit_squash(commits, None, '2.1', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=100)}
    assert footerless == ['abcdef1234' + '0' * 30]

  def test_no_land_aggregates_branch_only(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits = [self._commit('2.1', {'Opus 4.8': C(output=100)})]
    footer, _ = _emit_squash(commits, None, '2.1', s)
    parsed = _parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=100)}
