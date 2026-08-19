#!/usr/bin/env python
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import bro.llm.usage as usage
import bro.workflow.fold_branch as fold_branch
from bro.launch.hold import HOLD_VARIABLE


def _sh(repo, *args: str) -> str:
  return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def worktree(tmp_path, monkeypatch):
  """a four-commit feature branch over a pushed master, checked out and current.

  The task's own change (`one`, extended by `three`, then a review fix `four`)
  with an unrelated fix (`two`) committed in between it — the branch shape that
  wants to land as two commits.
  """
  origin, repo = tmp_path / 'origin.git', tmp_path / 'repo'
  subprocess.run(['git', 'init', '-q', '--bare', '-b', 'master', str(origin)], check=True)
  subprocess.run(['git', 'init', '-q', '-b', 'master', str(repo)], check=True)
  _sh(repo, 'config', 'user.email', 'test@example.com')
  _sh(repo, 'config', 'user.name', 'test')
  (repo / 'seed.txt').write_text('seed\n')
  _sh(repo, 'add', 'seed.txt')
  _sh(repo, 'commit', '-qm', 'seed')
  _sh(repo, 'remote', 'add', 'origin', str(origin))
  _sh(repo, 'push', '-q', 'origin', 'master')
  _sh(repo, 'checkout', '-q', '-b', 'feature')
  commits = []
  for day, (name, content, message) in enumerate(
    [
      ('feature.txt', 'one\n', 'add the feature'),
      ('unrelated.txt', 'two\n', 'fix the unrelated thing'),
      ('feature.txt', 'one\nthree\n', 'extend the feature'),
      ('feature.txt', 'one\nthree\nfour\n', 'address the review'),
    ],
    start=1,
  ):
    (repo / name).write_text(content)
    _sh(repo, 'add', name)
    _sh(repo, 'commit', '-qm', message, '--date', f'2026-01-0{day}T10:00:00+00:00')
    commits.append(_sh(repo, 'rev-parse', 'HEAD'))
  _sh(repo, 'push', '-q', '-u', 'origin', 'feature')
  monkeypatch.chdir(repo)
  return SimpleNamespace(path=repo, origin=origin, commits=commits)


TRAILER = 'Co-Authored-By: test <test@example.com>'


def _interactive_session(monkeypatch) -> None:
  monkeypatch.setenv(usage.SESSION_ID_VARIABLE, 'fold-test-session')
  monkeypatch.setenv(HOLD_VARIABLE, 'attended')


def _plan_file(tmp_path, *blocks: str) -> str:
  path = tmp_path / 'plan.txt'
  path.write_text('\n'.join(blocks))
  return str(path)


def _fold(commits, message: str = '') -> str:
  return f'fold {" ".join(commits)}\n{message}\n'


class TestLoadPlan:
  def test_folds_partition_the_branch_in_its_own_order(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    path = _plan_file(tmp_path, _fold([four, one, three]), _fold([two[:9]]))
    folds = fold_branch._load_plan(path, worktree.commits)
    assert folds[0].commits == (one, three, four)
    assert folds[1].commits == (two,)
    assert [fold.message for fold in folds] == ['add the feature', 'fix the unrelated thing']

  def test_one_fold_over_the_whole_branch_is_a_single_landed_commit(self, worktree, tmp_path):
    path = _plan_file(tmp_path, _fold(worktree.commits, 'land it as one'))
    folds = fold_branch._load_plan(path, worktree.commits)
    assert [(fold.commits, fold.message) for fold in folds] == [
      (tuple(worktree.commits), 'land it as one')
    ]

  def test_a_fold_naming_nothing_takes_the_whole_branch(self, worktree, tmp_path):
    path = _plan_file(tmp_path, 'fold\nland it as one\n')
    folds = fold_branch._load_plan(path, worktree.commits)
    assert [(fold.commits, fold.message) for fold in folds] == [
      (tuple(worktree.commits), 'land it as one')
    ]

  def test_a_fold_naming_nothing_takes_what_the_others_leave(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    path = _plan_file(tmp_path, 'fold\nthe feature\n', _fold([two]))
    folds = fold_branch._load_plan(path, worktree.commits)
    assert folds[0].commits == (one, three, four)
    assert folds[1].commits == (two,)

  def test_two_folds_naming_nothing_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, 'fold\nthe feature\n', 'fold\nthe rest\n')
    with pytest.raises(fold_branch.FoldError, match='folds 1, 2 each name no commits'):
      fold_branch._load_plan(path, worktree.commits)

  def test_a_fold_naming_nothing_with_nothing_left_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, _fold(worktree.commits), 'fold\nthe rest\n')
    with pytest.raises(fold_branch.FoldError, match='the rest are all claimed'):
      fold_branch._load_plan(path, worktree.commits)

  def test_the_lines_under_a_fold_are_its_message(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    message = 'land it\n\nthe body, over\ntwo lines\n\nTask: https://example/1'
    path = _plan_file(tmp_path, _fold([one, three, four], message), _fold([two]))
    folds = fold_branch._load_plan(path, worktree.commits)
    assert folds[0].message == message

  def test_a_message_line_opening_with_the_keyword_is_message_text(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    message = 'land it\n\nfold the review fixes into the unit they serve'
    path = _plan_file(tmp_path, _fold([one, three, four], message), _fold([two]))
    folds = fold_branch._load_plan(path, worktree.commits)
    assert folds[0].message == message

  def test_default_message_drops_the_accounting_footer(self, worktree, tmp_path):
    footer = '> created with bro//dev | Opus 5: ↑(1 0 0) ↓2'
    _sh(worktree.path, 'commit', '-q', '--allow-empty', '-m', f'add a thing\n\nbody\n\n{footer}')
    extra = _sh(worktree.path, 'rev-parse', 'HEAD')
    path = _plan_file(tmp_path, _fold([extra]), _fold(worktree.commits))
    folds = fold_branch._load_plan(path, [*worktree.commits, extra])
    assert folds[0].message == 'add a thing\n\nbody'

  def test_default_message_drops_an_inherited_co_author_trailer(self, worktree, tmp_path):
    _sh(worktree.path, 'commit', '-q', '--allow-empty', '-m', f'add a thing\n\nbody\n\n{TRAILER}')
    extra = _sh(worktree.path, 'rev-parse', 'HEAD')
    path = _plan_file(tmp_path, _fold([extra]), _fold(worktree.commits))
    folds = fold_branch._load_plan(path, [*worktree.commits, extra])
    assert folds[0].message == 'add a thing\n\nbody'

  def test_unlanded_commit_refused(self, worktree, tmp_path):
    one, two, three, _four = worktree.commits
    path = _plan_file(tmp_path, _fold([one, three]), _fold([two]))
    with pytest.raises(fold_branch.FoldError, match='unlanded'):
      fold_branch._load_plan(path, worktree.commits)

  def test_commit_claimed_twice_refused(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    path = _plan_file(tmp_path, _fold([one, three, four]), _fold([two, four]))
    with pytest.raises(fold_branch.FoldError, match='fold 1 and fold 2 both'):
      fold_branch._load_plan(path, worktree.commits)

  def test_commit_outside_the_branch_refused(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    seed = _sh(worktree.path, 'rev-parse', 'origin/master')
    path = _plan_file(tmp_path, _fold([one, three, four, seed]), _fold([two]))
    with pytest.raises(fold_branch.FoldError, match='the branch does not add'):
      fold_branch._load_plan(path, worktree.commits)

  def test_unknown_sha_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, _fold(['deadbee']), _fold(worktree.commits))
    with pytest.raises(fold_branch.FoldError, match='not a commit'):
      fold_branch._load_plan(path, worktree.commits)

  def test_text_before_the_first_fold_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, 'the plan:\n', _fold(worktree.commits[:1]))
    with pytest.raises(fold_branch.FoldError, match='expected a `fold'):
      fold_branch._load_plan(path, worktree.commits)

  def test_a_plan_without_folds_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, '')
    with pytest.raises(fold_branch.FoldError, match='no `fold'):
      fold_branch._load_plan(path, worktree.commits)


def _two_folds(worktree, messages=('the feature', 'the unrelated fix')):
  one, two, three, four = worktree.commits
  return [
    fold_branch.Fold(commits=(one, three, four), message=messages[0]),
    fold_branch.Fold(commits=(two,), message=messages[1]),
  ]


class TestRewrite:
  def test_folds_non_contiguous_commits_keeping_the_tree(self, worktree):
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    branch_tree = _sh(worktree.path, 'rev-parse', 'HEAD^{tree}')
    tip = fold_branch._rewrite(_two_folds(worktree), base, ['> footer one', '> footer two'])
    assert _sh(worktree.path, 'rev-parse', f'{tip}^{{tree}}') == branch_tree
    landed = _sh(worktree.path, 'log', '--reverse', '--format=%s', f'{base}..{tip}').splitlines()
    assert landed == ['the feature', 'the unrelated fix']
    assert _sh(worktree.path, 'log', '-1', '--format=%B', tip).endswith('> footer two')

  def test_an_interactive_session_co_authors_every_landed_commit(self, worktree, monkeypatch):
    _interactive_session(monkeypatch)
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    tip = fold_branch._rewrite(_two_folds(worktree), base, ['> footer one', ''])
    assert _sh(worktree.path, 'log', '--format=%B', f'{base}..{tip}').count(TRAILER) == 2
    assert _sh(worktree.path, 'log', '-1', '--format=%B', tip).endswith(TRAILER)

  def test_an_unattended_session_lands_the_commits_uncredited(self, worktree, monkeypatch):
    _interactive_session(monkeypatch)
    monkeypatch.setenv(HOLD_VARIABLE, 'unattended')
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    tip = fold_branch._rewrite(_two_folds(worktree), base, ['> footer one', ''])
    assert TRAILER not in _sh(worktree.path, 'log', '--format=%B', f'{base}..{tip}')

  def test_a_group_without_accounting_carries_no_footer(self, worktree):
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    tip = fold_branch._rewrite(_two_folds(worktree), base, ['> footer one', ''])
    assert _sh(worktree.path, 'log', '-1', '--format=%B', tip).strip() == 'the unrelated fix'

  def test_a_fold_keeps_the_authorship_of_the_work(self, worktree):
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    one, two, _three, _four = worktree.commits
    tip = fold_branch._rewrite(_two_folds(worktree), base, ['', ''])
    dates = _sh(worktree.path, 'log', '--reverse', '--format=%aI', f'{base}..{tip}').splitlines()
    assert dates == [
      _sh(worktree.path, 'log', '-1', '--format=%aI', one),
      _sh(worktree.path, 'log', '-1', '--format=%aI', two),
    ]

  def test_the_same_plan_rebuilds_the_same_commits(self, worktree):
    # a re-fold that regroups nothing must not churn the branch
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    head = _sh(worktree.path, 'rev-parse', 'HEAD')
    first = fold_branch._rewrite(_two_folds(worktree), base, ['> footer', ''])
    _sh(worktree.path, 'reset', '--hard', head)
    assert fold_branch._rewrite(_two_folds(worktree), base, ['> footer', '']) == first

  def test_a_grouping_that_collides_aborts_the_rebase(self, worktree):
    one, two, three, four = worktree.commits
    (worktree.path / 'unrelated.txt').write_text('two\nmore\n')
    _sh(worktree.path, 'add', 'unrelated.txt')
    _sh(worktree.path, 'commit', '-qm', 'extend the unrelated fix')
    five = _sh(worktree.path, 'rev-parse', 'HEAD')
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    # the second group's follow-up is hoisted above the commit it edits
    folds = [
      fold_branch.Fold(commits=(one, three, four, five), message='the feature'),
      fold_branch.Fold(commits=(two,), message='the unrelated fix'),
    ]
    with pytest.raises(fold_branch.FoldError, match='does not apply'):
      fold_branch._rewrite(folds, base, ['', ''])
    assert not (worktree.path / '.git' / 'rebase-merge').exists()


class TestFold:
  def _plan(self, worktree, tmp_path) -> str:
    unrelated = worktree.commits[1]
    return _plan_file(tmp_path, 'fold\nthe feature\n', _fold([unrelated], 'the unrelated fix'))

  def test_folds_the_branch_and_prints_what_it_carries(self, worktree, tmp_path, capsys):
    with (
      patch.object(fold_branch.commit_footer, 'group_footers', return_value=['> footer', '']),
      patch.object(fold_branch.commit_footer, 'record_session_spend') as recorded,
    ):
      assert fold_branch.fold_branch(self._plan(worktree, tmp_path), 'master') is None
    landed = _sh(worktree.path, 'log', '--reverse', '--format=%s', 'origin/master..HEAD')
    assert landed.splitlines() == ['the feature', 'the unrelated fix']
    recorded.assert_called_once_with()
    result = json.loads(capsys.readouterr().out)
    assert result['base'] == 'master'
    assert [commit['subject'] for commit in result['commits']] == [
      'the feature',
      'the unrelated fix',
    ]
    assert result['commits'][0]['sha'] == _sh(worktree.path, 'rev-parse', 'HEAD~1')

  def test_an_unaccounted_branch_advances_no_baseline(self, worktree, tmp_path):
    # a branch with no footered commits aggregates to nothing, and nothing to
    # attribute means no baseline to advance either
    with (
      patch.object(fold_branch.commit_footer, 'group_footers', return_value=['', '']),
      patch.object(fold_branch.commit_footer, 'record_session_spend') as recorded,
    ):
      assert fold_branch.fold_branch(self._plan(worktree, tmp_path), 'master') is None
    recorded.assert_not_called()

  def test_a_dirty_worktree_refuses_before_touching_the_branch(self, worktree, tmp_path):
    (worktree.path / 'feature.txt').write_text('uncommitted\n')
    head = _sh(worktree.path, 'rev-parse', 'HEAD')
    with pytest.raises(fold_branch.FoldError, match='uncommitted changes'):
      fold_branch._fold(self._plan(worktree, tmp_path), 'master')
    assert _sh(worktree.path, 'rev-parse', 'HEAD') == head

  def test_a_rewrite_that_changes_the_content_leaves_the_branch_alone(self, worktree, tmp_path):
    head = _sh(worktree.path, 'rev-parse', 'HEAD')
    with (
      patch.object(fold_branch.commit_footer, 'group_footers', return_value=['', '']),
      patch.object(
        fold_branch, '_rewrite', return_value=_sh(worktree.path, 'rev-parse', 'origin/master')
      ),
      pytest.raises(fold_branch.FoldError, match='changes the content'),
    ):
      fold_branch._fold(self._plan(worktree, tmp_path), 'master')
    assert _sh(worktree.path, 'rev-parse', 'HEAD') == head

  def test_a_branch_with_nothing_over_the_base_refuses(self, worktree, tmp_path):
    _sh(worktree.path, 'checkout', '-q', 'master')
    with pytest.raises(fold_branch.FoldError, match='nothing to fold'):
      fold_branch._fold(self._plan(worktree, tmp_path), 'master')

  def test_a_failed_fold_exits_nonzero_without_printing(self, worktree, tmp_path, capsys):
    (worktree.path / 'feature.txt').write_text('uncommitted\n')
    assert fold_branch.fold_branch(self._plan(worktree, tmp_path), 'master') == 1
    assert capsys.readouterr().out == ''
