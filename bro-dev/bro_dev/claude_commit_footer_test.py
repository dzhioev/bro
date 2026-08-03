#!/usr/bin/env python
import cw.claude_commit_footer
import llm.usage as usage
from cw.claude_commit_footer import (
  State,
  _effective_baseline,
  _emit_default,
  _emit_squash,
)

OPUS = 'claude-opus-4-8'


def test_repo_root_uses_the_operated_git_repository(tmp_path, monkeypatch):
  monkeypatch.setattr(cw.claude_commit_footer, 'project_root', lambda: tmp_path)
  assert cw.claude_commit_footer._repo_root() == tmp_path


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


class TestEffectiveBaseline:
  def test_normal_growth_uses_committed(self):
    committed = C(input=10, output=20)
    assert _effective_baseline(committed, C(input=15, output=30)) == committed

  def test_equal_uses_committed(self):
    committed = C(output=20)
    assert _effective_baseline(committed, C(output=20)) == committed

  def test_any_class_backwards_resets_to_zero(self):
    # output dropped (a new session reused the worktree) -> reset even though
    # input grew; a within-session cumulative could never go backwards in any class
    assert _effective_baseline(C(input=10, output=20), C(input=15, output=5)) == C()


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
    footer = _emit_default(usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=100)}), s)
    assert footer.startswith('> created with Claude Code 2.1 | ')
    assert 'Opus 4.8: ↑(0 0 0) ↓100' in footer
    # cum_now staged for promotion
    assert s.staged[OPUS] == C(output=100)

  def test_bro_agent_footer(self, tmp_path):
    s = State(tmp_path / 'state.json')
    footer = _emit_default(usage.Usage(agent='bro//ppp-dev', per_model={'gpt-5': C(output=42)}), s)
    assert footer == '> created with bro//ppp-dev | gpt-5: ↑(0 0 0) ↓42'

  def test_second_commit_is_delta(self, tmp_path):
    p = tmp_path / 'state.json'
    s = State(p)
    _emit_default(usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=100)}), s)
    s.record()
    footer = _emit_default(
      usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=130)}), State(p)
    )
    assert '↓30' in footer

  def test_usage_reset_recredits_full_cumulative(self, tmp_path):
    s = State(tmp_path / 'state.json')
    s.committed[OPUS] = C(output=5_000)  # a prior session's mark in this worktree
    # a new session reuses the worktree: cumulative reset, smaller than the mark
    footer = _emit_default(usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=200)}), s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=200)}  # full new cumulative, not negative

  def test_deltas_telescope_to_final_cumulative(self, tmp_path):
    p = tmp_path / 'state.json'
    cums = [C(output=100), C(output=130), C(output=175)]
    total = 0
    for cum in cums:
      s = State(p)
      footer = _emit_default(usage.Usage(agent='Claude Code 2.1', per_model={OPUS: cum}), s)
      parsed = usage.parse_footer(footer)
      assert parsed is not None
      total += parsed.delta['Opus 4.8']['output']
      s.record()
    assert total == 175  # == final cumulative


class TestEmitSquash:
  def _commit(self, agent, tokens) -> tuple[str, str]:
    return ('0' * 40, f'subject\n\n{usage.format_footer([agent], tokens)}\n')

  def test_auto_case_reduces_to_land_cumulative(self, tmp_path):
    # one session authored both branch commits and is also the land session;
    # branch deltas telescope to committed, remainder adds the /land work.
    p = tmp_path / 'state.json'
    s = State(p)
    s.committed[OPUS] = C(output=130)  # mark after the last branch commit
    commits = [
      self._commit('Claude Code 2.1', {'Opus 4.8': C(output=100)}),
      self._commit('Claude Code 2.1', {'Opus 4.8': C(output=30)}),
    ]
    land = usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=150)})
    footer, footerless = _emit_squash(commits, land, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=150)}  # 100 + 30 + (150 - 130)
    assert parsed.agents == ['Claude Code 2.1']
    assert footerless == []

  def test_unions_agents_sorted_and_sums_classes(self, tmp_path):
    s = State(tmp_path / 'state.json')  # land session authored no branch commits
    commits = [
      self._commit('Claude Code 2.1.120', {'Opus 4.8': C(input=100, output=1)}),
      self._commit('bro//ppp-dev', {'gpt-5': C(cache_read=50)}),
    ]
    land = usage.Usage(agent='Claude Code 2.1.130', per_model={OPUS: C(input=40)})
    footer, _ = _emit_squash(commits, land, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(input=140, output=1), 'gpt-5': C(cache_read=50)}
    assert parsed.agents == ['Claude Code 2.1.120', 'Claude Code 2.1.130', 'bro//ppp-dev']

  def test_historic_compressed_versions_sum(self, tmp_path):
    # commits made before the agent generalization carry the compressed shape;
    # their versions must still count and normalize to full agents.
    s = State(tmp_path / 'state.json')
    commits = [
      ('0' * 40, 'subject\n\n> created with Claude Code 2.1.114, 2.1.120 | Opus 4.8: ↑(1 0 0) ↓2\n')
    ]
    footer, _ = _emit_squash(commits, None, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(input=1, output=2)}
    assert parsed.agents == ['Claude Code 2.1.114', 'Claude Code 2.1.120']

  def test_footerless_commit_flagged_and_zero(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits: list[tuple[str, str]] = [
      self._commit('Claude Code 2.1', {'Opus 4.8': C(output=100)}),
      ('abcdef1234' + '0' * 30, 'chore: no footer\n\nbody\n'),
    ]
    footer, footerless = _emit_squash(commits, None, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=100)}
    assert footerless == ['abcdef1234' + '0' * 30]

  def test_no_land_aggregates_branch_only(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits = [self._commit('Claude Code 2.1', {'Opus 4.8': C(output=100)})]
    footer, _ = _emit_squash(commits, None, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=100)}
