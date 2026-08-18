#!/usr/bin/env python
import os
import subprocess

import bro.llm.usage as usage
from bro.workflow.commit_footer import (
  State,
  _aggregate,
  _append,
  _effective_baseline,
  _emit_default,
  _repo_root,
  group_footers,
  install_hooks,
  record_session_spend,
)

OPUS = 'claude-opus-4-8'


def _git(*args: str) -> None:
  subprocess.run(
    ['git', '-c', 'user.email=test@example.com', '-c', 'user.name=test', *args], check=True
  )


def test_repo_root_is_the_linked_worktree_not_the_main_checkout(tmp_path, monkeypatch):
  main = tmp_path / 'main'
  _git('init', '-q', '-b', 'master', str(main))
  (main / 'seed.txt').write_text('seed\n')
  _git('-C', str(main), 'add', 'seed.txt')
  _git('-C', str(main), 'commit', '-qm', 'seed')
  linked = tmp_path / 'linked'
  _git('-C', str(main), 'worktree', 'add', '-q', str(linked))

  monkeypatch.chdir(linked)
  assert _repo_root() == linked.resolve()


def test_install_hooks_copies_both_hooks_executable(tmp_path):
  _git('init', '-q', str(tmp_path))
  install_hooks(tmp_path)
  for hook_name in ('commit-msg', 'post-commit'):
    hook = tmp_path / '.git' / 'hooks' / hook_name
    assert hook.read_text().startswith('#!/usr/bin/env -S bash -e\n')
    assert os.access(hook, os.X_OK)


def C(input=0, cache_write=0, cache_read=0, output=0):
  return {'input': input, 'cache_write': cache_write, 'cache_read': cache_read, 'output': output}


def _parsed(footer: str) -> usage.Footer:
  parsed = usage.parse_footer(footer)
  assert parsed is not None
  return parsed


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

  def test_record_with_nothing_staged_keeps_the_baseline(self, tmp_path):
    # a footerless commit (a human's) still fires post-commit; the committed
    # mark must survive it or the next delta over-credits
    p = tmp_path / 'state.json'
    s = State(p)
    s.stage({OPUS: C(output=100)})
    s.record()
    State(p).record()
    assert State(p).committed == {OPUS: C(output=100)}

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
    footer = _emit_default(usage.Usage(agent='bro//dev', per_model={'gpt-5': C(output=42)}), s)
    assert footer == '> created with bro//dev | gpt-5: ↑(0 0 0) ↓42'

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


class TestAppend:
  def _message(self, tmp_path, text):
    path = tmp_path / 'COMMIT_EDITMSG'
    path.write_text(text)
    return path

  def _agent_environment(self, monkeypatch, output=100):
    monkeypatch.setenv(usage.SESSION_ID_VARIABLE, 'append-test-session')
    monkeypatch.setattr(
      usage,
      'current_usage',
      lambda: usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=output)}),
    )

  def test_no_usage_source_leaves_the_message(self, tmp_path, monkeypatch):
    monkeypatch.setattr(usage, 'current_usage', lambda: None)
    path = self._message(tmp_path, 'subject\n')
    state = State(tmp_path / 'state.json')
    _append(path, state)
    assert path.read_text() == 'subject\n'
    assert state.staged == {}

  def test_fallback_resolved_usage_without_env_marker_is_ignored(self, tmp_path, monkeypatch):
    # a human's shell can resolve usage through the working-directory transcript
    # fallback; only an env-keyed source marks an agent commit
    monkeypatch.setattr(
      usage,
      'current_usage',
      lambda: usage.Usage(agent='Claude Code 2.1', per_model={OPUS: C(output=100)}),
    )
    path = self._message(tmp_path, 'subject\n')
    state = State(tmp_path / 'state.json')
    _append(path, state)
    assert path.read_text() == 'subject\n'
    assert state.staged == {}

  def test_appends_footer_and_stages(self, tmp_path, monkeypatch):
    self._agent_environment(monkeypatch)
    path = self._message(tmp_path, 'subject\n\nbody\n')
    state = State(tmp_path / 'state.json')
    _append(path, state)
    message = path.read_text()
    assert message.startswith('subject\n\nbody\n\n> created with Claude Code 2.1 | ')
    assert message.endswith('↓100\n')
    assert state.staged[OPUS] == C(output=100)

  def test_footered_message_kept_verbatim(self, tmp_path, monkeypatch):
    # an amend or rebase reword re-runs the hook; the commit keeps its original
    # attribution instead of a recomputed (double-counting) delta
    self._agent_environment(monkeypatch)
    original = f'subject\n\n{usage.format_footer(["Claude Code 2.1"], {"Opus 4.8": C(output=7)})}\n'
    path = self._message(tmp_path, original)
    state = State(tmp_path / 'state.json')
    _append(path, state)
    assert path.read_text() == original
    assert state.staged == {}

  def test_empty_message_left_for_git_to_abort(self, tmp_path, monkeypatch):
    self._agent_environment(monkeypatch)
    path = self._message(tmp_path, '\n\n')
    state = State(tmp_path / 'state.json')
    _append(path, state)
    assert path.read_text() == '\n\n'
    assert state.staged == {}


class TestGroupFooters:
  def _repo_with_commits(self, tmp_path, messages) -> list[str]:
    _git('init', '-q', '-b', 'master', str(tmp_path))
    shas = []
    for index, message in enumerate(messages):
      (tmp_path / f'f{index}.txt').write_text('x\n')
      _git('-C', str(tmp_path), 'add', f'f{index}.txt')
      _git('-C', str(tmp_path), 'commit', '-qm', message)
      shas.append(
        subprocess.check_output(
          ['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True
        ).strip()
      )
    return shas

  def _footer(self, output):
    return usage.format_footer(['Claude Code 2.1'], {'Opus 4.8': C(output=output)})

  def test_unaccounted_commits_aggregate_to_nothing(self, tmp_path, monkeypatch, caplog):
    shas = self._repo_with_commits(tmp_path, ['seed', 'one', 'two'])
    monkeypatch.chdir(tmp_path)
    assert group_footers([shas[1:2], shas[2:3]]) == ['', '']
    assert caplog.text == ''

  def test_aggregates_per_group_and_flags_the_footerless(self, tmp_path, monkeypatch, caplog):
    shas = self._repo_with_commits(
      tmp_path, ['seed', f'one\n\n{self._footer(9)}', 'two', f'three\n\n{self._footer(4)}']
    )
    monkeypatch.chdir(tmp_path)
    footers = group_footers([[shas[1], shas[2]], [shas[3]]])
    assert _parsed(footers[0]).delta == {'Opus 4.8': C(output=9)}
    assert _parsed(footers[1]).delta == {'Opus 4.8': C(output=4)}
    assert 'without a parseable footer' in caplog.text
    assert 'no session usage' in caplog.text

  def test_a_group_without_accounting_carries_no_footer(self, tmp_path, monkeypatch):
    shas = self._repo_with_commits(tmp_path, ['seed', f'one\n\n{self._footer(9)}', 'two'])
    monkeypatch.chdir(tmp_path)
    assert group_footers([[shas[1]], [shas[2]]])[1] == ''

  def test_grouping_preserves_the_total(self, tmp_path, monkeypatch):
    # the accounting contract: however the branch is grouped, the landed footers
    # sum to what one folded commit would have carried
    shas = self._repo_with_commits(
      tmp_path,
      [
        'seed',
        f'one\n\n{self._footer(9)}',
        f'two\n\n{self._footer(4)}',
        f'three\n\n{self._footer(2)}',
      ],
    )
    monkeypatch.chdir(tmp_path)
    session = usage.Usage(agent='bro//dev', per_model={OPUS: C(output=20)})
    monkeypatch.setattr(usage, 'current_usage', lambda: session)
    whole = group_footers([shas[1:]])
    split = group_footers([[shas[1], shas[3]], [shas[2]]])
    total = usage.zero()
    for footer in split:
      total = usage.add(total, _parsed(footer).delta['Opus 4.8'])
    assert total == _parsed(whole[0]).delta['Opus 4.8'] == C(output=35)
    assert _parsed(split[1]).agents == ['Claude Code 2.1', 'bro//dev']  # the remainder
    assert _parsed(split[0]).agents == ['Claude Code 2.1']

  def test_the_remainder_rides_the_last_accounted_group(self, tmp_path, monkeypatch):
    shas = self._repo_with_commits(tmp_path, ['seed', f'one\n\n{self._footer(9)}', 'two'])
    monkeypatch.chdir(tmp_path)
    session = usage.Usage(agent='bro//dev', per_model={OPUS: C(output=20)})
    monkeypatch.setattr(usage, 'current_usage', lambda: session)
    footers = group_footers([[shas[1]], [shas[2]]])
    assert _parsed(footers[0]).delta == {'Opus 4.8': C(output=29)}
    assert footers[1] == ''

  def test_recording_the_session_spend_zeroes_the_next_remainder(self, tmp_path, monkeypatch):
    # a re-run must not credit the landing session's spend to a second commit
    shas = self._repo_with_commits(tmp_path, ['seed', f'one\n\n{self._footer(9)}'])
    monkeypatch.chdir(tmp_path)
    session = usage.Usage(agent='bro//dev', per_model={OPUS: C(output=20)})
    monkeypatch.setattr(usage, 'current_usage', lambda: session)
    assert _parsed(group_footers([[shas[1]]])[0]).delta == {'Opus 4.8': C(output=29)}
    record_session_spend()
    assert _parsed(group_footers([[shas[1]]])[0]).delta == {'Opus 4.8': C(output=9)}


class TestAggregate:
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
    footer = _aggregate(commits, land, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=150)}  # 100 + 30 + (150 - 130)
    assert parsed.agents == ['Claude Code 2.1']

  def test_unions_agents_sorted_and_sums_classes(self, tmp_path):
    s = State(tmp_path / 'state.json')  # land session authored no branch commits
    commits = [
      self._commit('Claude Code 2.1.120', {'Opus 4.8': C(input=100, output=1)}),
      self._commit('bro//dev', {'gpt-5': C(cache_read=50)}),
    ]
    land = usage.Usage(agent='Claude Code 2.1.130', per_model={OPUS: C(input=40)})
    footer = _aggregate(commits, land, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(input=140, output=1), 'gpt-5': C(cache_read=50)}
    assert parsed.agents == ['Claude Code 2.1.120', 'Claude Code 2.1.130', 'bro//dev']

  def test_historic_compressed_versions_sum(self, tmp_path):
    # commits made before the agent generalization carry the compressed shape;
    # their versions must still count and normalize to full agents.
    s = State(tmp_path / 'state.json')
    commits = [
      ('0' * 40, 'subject\n\n> created with Claude Code 2.1.114, 2.1.120 | Opus 4.8: ↑(1 0 0) ↓2\n')
    ]
    footer = _aggregate(commits, None, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(input=1, output=2)}
    assert parsed.agents == ['Claude Code 2.1.114', 'Claude Code 2.1.120']

  def test_footerless_commit_counts_zero(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits: list[tuple[str, str]] = [
      self._commit('Claude Code 2.1', {'Opus 4.8': C(output=100)}),
      ('abcdef1234' + '0' * 30, 'chore: no footer\n\nbody\n'),
    ]
    footer = _aggregate(commits, None, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=100)}

  def test_no_session_aggregates_the_commits_only(self, tmp_path):
    s = State(tmp_path / 'state.json')
    commits = [self._commit('Claude Code 2.1', {'Opus 4.8': C(output=100)})]
    footer = _aggregate(commits, None, s)
    parsed = usage.parse_footer(footer)
    assert parsed is not None
    assert parsed.delta == {'Opus 4.8': C(output=100)}
