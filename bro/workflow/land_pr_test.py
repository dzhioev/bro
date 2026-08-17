#!/usr/bin/env python
import contextlib
import json
import subprocess
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest

import bro.workflow.land_pr as land_pr


def _pr(**overrides: Any) -> dict[str, Any]:
  pr: dict[str, Any] = {
    'number': 310,
    'title': 'ride: land in one shot',
    'body': '## Test plan\n- [x] suite green',
    'state': 'OPEN',
    'reviewDecision': 'APPROVED',
    'baseRefName': 'master',
    'headRefName': 'worktree-feature',
    'url': 'https://github.com/o/r/pull/310',
    'statusCheckRollup': [],
  }
  pr.update(overrides)
  return pr


def _check(name: str = 'tests', status: str = 'COMPLETED', conclusion: str = 'SUCCESS') -> dict:
  return {'__typename': 'CheckRun', 'name': name, 'status': status, 'conclusion': conclusion}


def _status_context(context: str = 'ci/legacy', state: str = 'SUCCESS') -> dict[str, Any]:
  return {'__typename': 'StatusContext', 'context': context, 'state': state}


def _land(**overrides: Any) -> Optional[int]:
  arguments: dict[str, Any] = {
    'no_review': False,
    'allow_unchecked': False,
    'ignore_checks': False,
    'wait_checks': 0,
    'plan': None,
  }
  arguments.update(overrides)
  return land_pr.land_pr(**arguments)


class TestUncheckedBoxes:
  def test_finds_pending_items(self):
    body = '\n'.join(
      [
        '## Test plan',
        '- [x] done item',
        '- [ ] pending item',
        '  - [ ] nested pending',
        '* [ ] star pending',
        '- [] not a box',
      ]
    )
    assert land_pr._unchecked_boxes(body) == ['pending item', 'nested pending', 'star pending']

  def test_clean_body(self):
    assert land_pr._unchecked_boxes('## Test plan\n- [x] all good') == []


class TestBodyWithFooter:
  def test_appends_after_blank_line(self):
    assert land_pr._body_with_footer('body\n', '> footer') == 'body\n\n> footer'

  def test_empty_body_is_footer_alone(self):
    assert land_pr._body_with_footer('  \n', '> footer') == '> footer'

  def test_absent_footer_keeps_the_trimmed_body(self):
    assert land_pr._body_with_footer('body\n', '') == 'body'


class TestPreconditionError:
  def test_open_approved_checked_passes(self):
    assert land_pr._precondition_error(_pr(), False, False) is None

  def test_not_open(self):
    error = land_pr._precondition_error(_pr(state='MERGED'), False, False)
    assert error is not None and 'MERGED' in error

  def test_not_approved(self):
    error = land_pr._precondition_error(_pr(reviewDecision=''), False, False)
    assert error is not None and '--no-review' in error

  def test_no_review_waives_missing_approval(self):
    assert land_pr._precondition_error(_pr(reviewDecision=''), True, False) is None

  def test_changes_requested_refused_despite_no_review(self):
    error = land_pr._precondition_error(_pr(reviewDecision='CHANGES_REQUESTED'), True, True)
    assert error is not None and 'changes requested' in error

  def test_unchecked_boxes(self):
    pr = _pr(body='## Test plan\n- [ ] verify manually')
    error = land_pr._precondition_error(pr, False, False)
    assert error is not None and 'verify manually' in error

  def test_allow_unchecked_waives_boxes(self):
    pr = _pr(body='## Test plan\n- [ ] verify manually')
    assert land_pr._precondition_error(pr, False, True) is None


def _fake_run(merge_calls: list[list[str]], pr: dict[str, Any]):
  def run(command: list[str], *, capture: bool) -> str:
    assert capture == (command[:3] != ['gh', 'pr', 'merge'])
    if command[:3] == ['gh', 'pr', 'view'] and 'mergeCommit' in command[-1]:
      merged = {
        'state': 'MERGED',
        'mergeCommit': {'oid': 'abc123'},
        'mergedAt': '2026-07-03T10:41:02Z',
      }
      return json.dumps(merged)
    if command[:3] == ['gh', 'pr', 'view']:
      return json.dumps(pr)
    if command[:3] == ['gh', 'pr', 'merge']:
      merge_calls.append(command)
      return ''
    raise AssertionError(f'unexpected command: {command}')

  return run


@contextlib.contextmanager
def _landing(pr: dict[str, Any], footer: str = '> created with Claude Code …'):
  """the squash path with the branch's commits and their accounting stubbed —
  what land-pr says to gh is what these tests read."""
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr, '_branch_commits', return_value=['c0ffee1', 'c0ffee2']),
    patch.object(land_pr.commit_footer, 'group_footers', return_value=[footer]),
    patch.object(land_pr.commit_footer, 'record_session_spend') as recorded,
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)) as spawned,
  ):
    yield merge_calls, spawned, recorded


def test_land_happy_path(capsys):
  with _landing(_pr()) as (merge_calls, spawned, recorded):
    assert _land() is None

  assert len(merge_calls) == 1
  merge = merge_calls[0]
  assert merge[:5] == ['gh', 'pr', 'merge', '310', '--squash']
  assert merge[merge.index('--subject') + 1] == 'ride: land in one shot'
  expected_body = '## Test plan\n- [x] suite green\n\n> created with Claude Code …'
  assert merge[merge.index('--body') + 1] == expected_body
  spawned.assert_called_once_with(
    ['git', 'push', 'origin', '--delete', 'worktree-feature'], text=True
  )
  recorded.assert_called_once_with()

  assert json.loads(capsys.readouterr().out) == {
    'pr': 310,
    'url': 'https://github.com/o/r/pull/310',
    'title': 'ride: land in one shot',
    'base': 'master',
    'merged_sha': 'abc123',
    'merged_at': '2026-07-03T10:41:02Z',
    'commits': 1,
    'branch_deleted': True,
  }


def test_land_refuses_unapproved_without_merging(capsys):
  merge_calls: list[list[str]] = []
  fake = _fake_run(merge_calls, _pr(reviewDecision='REVIEW_REQUIRED'))
  with patch.object(land_pr, '_run', side_effect=fake):
    assert _land() == 1
  assert merge_calls == []
  assert capsys.readouterr().out == ''


def test_land_failed_branch_delete_degrades(capsys):
  with _landing(_pr()) as (_, spawned, _recorded):
    spawned.return_value = subprocess.CompletedProcess([], 1)
    assert _land() is None
  assert json.loads(capsys.readouterr().out)['branch_deleted'] is False


def test_land_with_an_unaccounted_branch_keeps_the_pr_body():
  # a branch with no footered commits aggregates to nothing, and nothing to
  # attribute means no baseline to advance either
  with _landing(_pr(), footer='') as (merge_calls, _spawned, recorded):
    assert _land() is None
  merge = merge_calls[0]
  assert merge[merge.index('--body') + 1] == '## Test plan\n- [x] suite green'
  recorded.assert_not_called()


class TestSplitChecks:
  def test_no_checks_is_clean(self):
    assert land_pr._split_checks([]) == ([], [])

  def test_running_check_is_pending(self):
    pending, failed = land_pr._split_checks([_check(status='IN_PROGRESS', conclusion='')])
    assert (pending, failed) == (['tests'], [])

  def test_queued_check_is_pending(self):
    pending, _ = land_pr._split_checks([_check(status='QUEUED', conclusion='')])
    assert pending == ['tests']

  def test_failed_conclusions_are_failures(self):
    for conclusion in ('FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED'):
      _, failed = land_pr._split_checks([_check(conclusion=conclusion)])
      assert failed == ['tests'], conclusion

  def test_neutral_and_skipped_pass(self):
    for conclusion in ('SUCCESS', 'NEUTRAL', 'SKIPPED'):
      assert land_pr._split_checks([_check(conclusion=conclusion)]) == ([], []), conclusion

  def test_legacy_status_contexts(self):
    assert land_pr._split_checks([_status_context(state='PENDING')]) == (['ci/legacy'], [])
    assert land_pr._split_checks([_status_context(state='SUCCESS')]) == ([], [])
    assert land_pr._split_checks([_status_context(state='ERROR')]) == ([], ['ci/legacy'])


class TestChecksError:
  def test_clean_rollup_passes(self):
    assert land_pr._checks_error(310, [_check()]) is None

  def test_pending_refuses_and_names_the_check(self):
    error = land_pr._checks_error(310, [_check(status='IN_PROGRESS')])
    assert error is not None and 'tests' in error

  def test_failure_refuses_and_names_the_check(self):
    error = land_pr._checks_error(310, [_check(conclusion='FAILURE')])
    assert error is not None and 'tests' in error


class TestAwaitChecks:
  def test_concluded_rollup_returns_at_once(self):
    with patch.object(land_pr, '_pr_view') as view:
      assert land_pr._await_checks(310, [_check()], 480) == [_check()]
    view.assert_not_called()

  def test_polls_until_the_checks_conclude(self):
    later = {'statusCheckRollup': [_check(status='COMPLETED', conclusion='SUCCESS')]}
    with (
      patch.object(land_pr, '_pr_view', return_value=later) as view,
      patch.object(land_pr.time, 'sleep') as sleep,
    ):
      rollup = land_pr._await_checks(310, [_check(status='IN_PROGRESS')], 480)
    assert land_pr._split_checks(rollup) == ([], [])
    view.assert_called_once_with(['statusCheckRollup'], number=310)
    sleep.assert_called_once_with(land_pr._CHECK_POLL_INTERVAL)

  def test_zero_budget_does_not_wait(self):
    pending = [_check(status='IN_PROGRESS')]
    with patch.object(land_pr, '_pr_view') as view:
      assert land_pr._await_checks(310, pending, 0) == pending
    view.assert_not_called()


def test_land_refuses_pending_checks_without_merging(capsys):
  merge_calls: list[list[str]] = []
  pr = _pr(statusCheckRollup=[_check(status='IN_PROGRESS', conclusion='')])
  with patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)):
    assert _land() == 1
  assert merge_calls == []
  assert capsys.readouterr().out == ''


def test_land_refuses_a_failed_check():
  merge_calls: list[list[str]] = []
  pr = _pr(statusCheckRollup=[_check(conclusion='FAILURE')])
  with patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)):
    assert _land() == 1
  assert merge_calls == []


def test_ignore_checks_merges_past_a_failure_and_names_it(caplog):
  pr = _pr(statusCheckRollup=[_check(conclusion='FAILURE'), _check('lint', status='QUEUED')])
  with _landing(pr) as (merge_calls, _spawned, _recorded):
    assert _land(ignore_checks=True) is None
  assert len(merge_calls) == 1
  assert 'tests' in caplog.text and 'lint' in caplog.text


def test_ignore_checks_does_not_wait():
  pr = _pr(statusCheckRollup=[_check(status='IN_PROGRESS', conclusion='')])
  with _landing(pr), patch.object(land_pr.time, 'sleep') as sleep:
    assert _land(ignore_checks=True, wait_checks=480) is None
  sleep.assert_not_called()


def test_land_merges_with_green_checks():
  pr = _pr(statusCheckRollup=[_check(), _status_context()])
  with _landing(pr) as (merge_calls, _spawned, _recorded):
    assert _land() is None
  assert len(merge_calls) == 1


def test_land_does_not_wait_for_checks_on_an_unapproved_pr():
  merge_calls: list[list[str]] = []
  pr = _pr(reviewDecision='', statusCheckRollup=[_check(status='IN_PROGRESS', conclusion='')])
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr.time, 'sleep') as sleep,
  ):
    assert _land(wait_checks=480) == 1
  sleep.assert_not_called()


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
    folds = land_pr._load_plan(path, worktree.commits)
    assert folds[0].commits == (one, three, four)
    assert folds[1].commits == (two,)
    assert [fold.message for fold in folds] == ['add the feature', 'fix the unrelated thing']

  def test_the_lines_under_a_fold_are_its_message(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    message = 'land it\n\nthe body, over\ntwo lines\n\nTask: https://example/1'
    path = _plan_file(tmp_path, _fold([one, three, four], message), _fold([two]))
    folds = land_pr._load_plan(path, worktree.commits)
    assert folds[0].message == message

  def test_a_message_line_opening_with_the_keyword_is_message_text(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    message = 'land it\n\nfold the review fixes into the unit they serve'
    path = _plan_file(tmp_path, _fold([one, three, four], message), _fold([two]))
    folds = land_pr._load_plan(path, worktree.commits)
    assert folds[0].message == message

  def test_default_message_drops_the_accounting_footer(self, worktree, tmp_path):
    footer = '> created with bro//dev | Opus 5: ↑(1 0 0) ↓2'
    _sh(worktree.path, 'commit', '-q', '--allow-empty', '-m', f'add a thing\n\nbody\n\n{footer}')
    extra = _sh(worktree.path, 'rev-parse', 'HEAD')
    path = _plan_file(tmp_path, _fold([extra]), _fold(worktree.commits))
    folds = land_pr._load_plan(path, [*worktree.commits, extra])
    assert folds[0].message == 'add a thing\n\nbody'

  def test_unlanded_commit_refused(self, worktree, tmp_path):
    one, two, three, _four = worktree.commits
    path = _plan_file(tmp_path, _fold([one, three]), _fold([two]))
    with pytest.raises(land_pr.LandError, match='unlanded'):
      land_pr._load_plan(path, worktree.commits)

  def test_commit_claimed_twice_refused(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    path = _plan_file(tmp_path, _fold([one, three, four]), _fold([two, four]))
    with pytest.raises(land_pr.LandError, match='fold 1 and fold 2 both'):
      land_pr._load_plan(path, worktree.commits)

  def test_commit_outside_the_branch_refused(self, worktree, tmp_path):
    one, two, three, four = worktree.commits
    seed = _sh(worktree.path, 'rev-parse', 'origin/master')
    path = _plan_file(tmp_path, _fold([one, three, four, seed]), _fold([two]))
    with pytest.raises(land_pr.LandError, match='does not add to its base'):
      land_pr._load_plan(path, worktree.commits)

  def test_unknown_sha_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, _fold(['deadbee']), _fold(worktree.commits))
    with pytest.raises(land_pr.LandError, match='not a commit'):
      land_pr._load_plan(path, worktree.commits)

  def test_single_fold_refused_as_the_plain_squash(self, worktree, tmp_path):
    path = _plan_file(tmp_path, _fold(worktree.commits))
    with pytest.raises(land_pr.LandError, match='without --plan'):
      land_pr._load_plan(path, worktree.commits)

  def test_text_before_the_first_fold_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, 'the plan:\n', _fold(worktree.commits[:1]))
    with pytest.raises(land_pr.LandError, match='expected a `fold'):
      land_pr._load_plan(path, worktree.commits)

  def test_a_plan_without_folds_refused(self, worktree, tmp_path):
    path = _plan_file(tmp_path, '')
    with pytest.raises(land_pr.LandError, match='no `fold'):
      land_pr._load_plan(path, worktree.commits)


def _two_folds(worktree, messages=('the feature', 'the unrelated fix')):
  one, two, three, four = worktree.commits
  return [
    land_pr.Fold(commits=(one, three, four), message=messages[0]),
    land_pr.Fold(commits=(two,), message=messages[1]),
  ]


class TestRewrite:
  def test_folds_non_contiguous_commits_keeping_the_tree(self, worktree):
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    reviewed_tree = _sh(worktree.path, 'rev-parse', 'HEAD^{tree}')
    tip = land_pr._rewrite(_two_folds(worktree), base, ['> footer one', '> footer two'])
    assert _sh(worktree.path, 'rev-parse', f'{tip}^{{tree}}') == reviewed_tree
    landed = _sh(worktree.path, 'log', '--reverse', '--format=%s', f'{base}..{tip}').splitlines()
    assert landed == ['the feature', 'the unrelated fix']
    assert _sh(worktree.path, 'log', '-1', '--format=%B', tip).endswith('> footer two')

  def test_a_group_without_accounting_carries_no_footer(self, worktree):
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    tip = land_pr._rewrite(_two_folds(worktree), base, ['> footer one', ''])
    assert _sh(worktree.path, 'log', '-1', '--format=%B', tip).strip() == 'the unrelated fix'

  def test_a_fold_keeps_the_authorship_of_the_work(self, worktree):
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    one, two, _three, _four = worktree.commits
    tip = land_pr._rewrite(_two_folds(worktree), base, ['', ''])
    dates = _sh(worktree.path, 'log', '--reverse', '--format=%aI', f'{base}..{tip}').splitlines()
    assert dates == [
      _sh(worktree.path, 'log', '-1', '--format=%aI', one),
      _sh(worktree.path, 'log', '-1', '--format=%aI', two),
    ]

  def test_the_same_plan_rebuilds_the_same_commits(self, worktree):
    # a land retried after a failed merge must not churn the branch
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    head = _sh(worktree.path, 'rev-parse', 'HEAD')
    first = land_pr._rewrite(_two_folds(worktree), base, ['> footer', ''])
    _sh(worktree.path, 'reset', '--hard', head)
    assert land_pr._rewrite(_two_folds(worktree), base, ['> footer', '']) == first

  def test_a_grouping_that_collides_aborts_the_rebase(self, worktree):
    one, two, three, four = worktree.commits
    (worktree.path / 'unrelated.txt').write_text('two\nmore\n')
    _sh(worktree.path, 'add', 'unrelated.txt')
    _sh(worktree.path, 'commit', '-qm', 'extend the unrelated fix')
    five = _sh(worktree.path, 'rev-parse', 'HEAD')
    base = _sh(worktree.path, 'rev-parse', 'origin/master')
    # the second group's follow-up is hoisted above the commit it edits
    folds = [
      land_pr.Fold(commits=(one, three, four, five), message='the feature'),
      land_pr.Fold(commits=(two,), message='the unrelated fix'),
    ]
    with pytest.raises(land_pr.LandError, match='does not apply'):
      land_pr._rewrite(folds, base, ['', ''])
    assert not (worktree.path / '.git' / 'rebase-merge').exists()


def _fake_split_run(merge_calls: list[list[str]], worktree, pr: dict[str, Any]):
  def run(command: list[str], *, capture: bool) -> str:
    if command[:2] == ['gh', 'api']:
      return 'true'
    if command[:3] == ['gh', 'pr', 'view'] and command[-1] == 'headRefOid':
      return json.dumps({'headRefOid': _sh(worktree.origin, 'rev-parse', 'feature')})
    if command[:3] == ['gh', 'pr', 'view']:
      return json.dumps(pr)
    if command[:3] == ['gh', 'pr', 'merge']:
      merge_calls.append(command)
      return ''
    raise AssertionError(f'unexpected command: {command}')

  return run


class TestRebaseMerge:
  def _pr(self, worktree) -> dict[str, Any]:
    return _pr(headRefName='feature', headRefOid=_sh(worktree.origin, 'rev-parse', 'feature'))

  def test_publishes_the_fold_and_merges_it(self, worktree):
    merge_calls: list[list[str]] = []
    with (
      patch.object(
        land_pr, '_run', side_effect=_fake_split_run(merge_calls, worktree, self._pr(worktree))
      ),
      patch.object(land_pr.commit_footer, 'record_session_spend') as recorded,
    ):
      land_pr._rebase_merge(
        self._pr(worktree), _two_folds(worktree), ['> footer', ''], False, False
      )
    tip = _sh(worktree.path, 'rev-parse', 'HEAD')
    assert _sh(worktree.origin, 'rev-parse', 'feature') == tip
    landed = _sh(worktree.path, 'log', '--reverse', '--format=%s', f'origin/master..{tip}')
    assert landed.splitlines() == ['the feature', 'the unrelated fix']
    assert merge_calls == [['gh', 'pr', 'merge', '310', '--rebase', '--match-head-commit', tip]]
    recorded.assert_called_once_with()

  def test_a_fold_that_changes_the_reviewed_content_publishes_nothing(self, worktree):
    (worktree.path / 'feature.txt').write_text('smuggled\n')
    _sh(worktree.path, 'add', 'feature.txt')
    _sh(worktree.path, 'commit', '-qm', 'sneak a change past review')
    head = _sh(worktree.path, 'rev-parse', 'HEAD')
    one, two, three, four = worktree.commits
    folds = [
      land_pr.Fold(commits=(one, three, four, head), message='the feature'),
      land_pr.Fold(commits=(two,), message='the unrelated fix'),
    ]
    published = _sh(worktree.origin, 'rev-parse', 'feature')
    with (
      patch.object(land_pr, '_run', side_effect=_fake_split_run([], worktree, self._pr(worktree))),
      pytest.raises(land_pr.LandError, match='changes the content'),
    ):
      land_pr._rebase_merge(self._pr(worktree), folds, ['', ''], False, False)
    assert _sh(worktree.origin, 'rev-parse', 'feature') == published
    assert _sh(worktree.path, 'rev-parse', 'HEAD') == head

  def test_a_dirty_worktree_refuses_before_touching_the_branch(self, worktree):
    (worktree.path / 'feature.txt').write_text('uncommitted\n')
    head = _sh(worktree.path, 'rev-parse', 'HEAD')
    with (
      patch.object(land_pr, '_run', side_effect=_fake_split_run([], worktree, self._pr(worktree))),
      pytest.raises(land_pr.LandError, match='uncommitted changes'),
    ):
      land_pr._rebase_merge(self._pr(worktree), _two_folds(worktree), ['', ''], False, False)
    assert _sh(worktree.path, 'rev-parse', 'HEAD') == head


def test_land_with_a_plan_folds_the_branch_as_planned(worktree, tmp_path, capsys):
  one, two, three, four = worktree.commits
  plan = _plan_file(tmp_path, _fold([one, three, four]), _fold([two]))
  merge_calls: list[list[str]] = []
  pr = _pr(headRefName='feature', headRefOid=_sh(worktree.origin, 'rev-parse', 'feature'))
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr, '_rebase_merge') as rebase_merge,
  ):
    assert _land(plan=plan) is None
  folds = rebase_merge.call_args.args[1]
  assert [fold.commits for fold in folds] == [(one, three, four), (two,)]
  assert json.loads(capsys.readouterr().out)['commits'] == 2
