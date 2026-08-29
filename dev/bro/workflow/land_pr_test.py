#!/usr/bin/env python
import contextlib
import json
import subprocess
from typing import Any, Optional
from unittest.mock import patch

import pytest

import bro.workflow.land_pr as land_pr

_HEAD_OID = 'c0ffee1234567890c0ffee1234567890c0ffee12'


def _check(name: str = 'tests', status: str = 'COMPLETED', conclusion: str = 'SUCCESS') -> dict:
  return {'__typename': 'CheckRun', 'name': name, 'status': status, 'conclusion': conclusion}


def _pr(**overrides: Any) -> dict[str, Any]:
  pr: dict[str, Any] = {
    'number': 310,
    'title': 'ride: land in one shot',
    'body': '## Test plan\n- [x] suite green',
    'state': 'OPEN',
    'reviewDecision': 'APPROVED',
    'baseRefName': 'master',
    'headRefName': 'worktree-feature',
    'headRefOid': _HEAD_OID,
    'url': 'https://github.com/o/r/pull/310',
    'commits': [{'oid': 'aaa1111'}, {'oid': 'bbb2222'}],
    'statusCheckRollup': [_check()],
  }
  pr.update(overrides)
  return pr


def _status_context(context: str = 'ci/legacy', state: str = 'SUCCESS') -> dict[str, Any]:
  return {'__typename': 'StatusContext', 'context': context, 'state': state}


@pytest.fixture(autouse=True)
def worktree_head():
  """the worktree sitting on the reviewed head, which is what land-pr requires."""
  with patch.object(land_pr, 'git_out', return_value=_HEAD_OID) as head:
    yield head


def _land(**overrides: Any) -> Optional[int]:
  arguments: dict[str, Any] = {
    'allow_unchecked': False,
    'ignore_checks': False,
    'wait_checks': 0,
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


class TestPreconditionError:
  def test_open_approved_checked_passes(self):
    assert land_pr._precondition_error(_pr(), False) is None

  def test_not_open(self):
    error = land_pr._precondition_error(_pr(state='MERGED'), False)
    assert error is not None and 'MERGED' in error

  def test_a_base_asking_for_no_review_lands_unreviewed(self):
    assert land_pr._precondition_error(_pr(reviewDecision=''), False) is None

  def test_changes_requested_is_refused(self):
    error = land_pr._precondition_error(_pr(reviewDecision='CHANGES_REQUESTED'), False)
    assert error is not None and 'changes requested' in error

  def test_a_review_the_base_requires_is_refused(self):
    error = land_pr._precondition_error(_pr(reviewDecision='REVIEW_REQUIRED'), False)
    assert error is not None and 'REVIEW_REQUIRED' in error

  def test_unchecked_boxes(self):
    pr = _pr(body='## Test plan\n- [ ] verify manually')
    error = land_pr._precondition_error(pr, False)
    assert error is not None and 'verify manually' in error

  def test_allow_unchecked_waives_boxes(self):
    pr = _pr(body='## Test plan\n- [ ] verify manually')
    assert land_pr._precondition_error(pr, True) is None


class TestHeadError:
  def test_the_reviewed_head_passes(self):
    assert land_pr._head_error(_pr()) is None

  def test_a_worktree_elsewhere_is_named(self, worktree_head):
    worktree_head.return_value = 'dead' * 10
    error = land_pr._head_error(_pr())
    assert error is not None and 'c0ffee123' in error and 'deaddeadd' in error


def _fake_run(merge_calls: list[list[str]], pr: dict[str, Any], rebase_merge: str = 'true'):
  def run(command: list[str], *, capture: bool) -> str:
    assert capture == (command[:3] != ['gh', 'pr', 'merge'])
    if command[:2] == ['gh', 'api']:
      return rebase_merge
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
def _landing(pr: dict[str, Any], rebase_merge: str = 'true'):
  """a land with the remote stubbed — what land-pr says to gh is what these
  tests read."""
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr, rebase_merge)),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)) as spawned,
  ):
    yield merge_calls, spawned


def test_land_happy_path(capsys):
  with _landing(_pr()) as (merge_calls, spawned):
    assert _land() is None

  assert merge_calls == [['gh', 'pr', 'merge', '310', '--rebase', '--match-head-commit', _HEAD_OID]]
  spawned.assert_called_once_with(
    ['git', 'push', 'origin', '--delete', 'worktree-feature'], text=True
  )

  assert json.loads(capsys.readouterr().out) == {
    'pr': 310,
    'url': 'https://github.com/o/r/pull/310',
    'title': 'ride: land in one shot',
    'base': 'master',
    'merged_sha': 'abc123',
    'merged_at': '2026-07-03T10:41:02Z',
    'commits': 2,
    'branch_deleted': True,
  }


def test_land_refuses_a_review_the_base_requires(capsys):
  merge_calls: list[list[str]] = []
  fake = _fake_run(merge_calls, _pr(reviewDecision='REVIEW_REQUIRED'))
  with patch.object(land_pr, '_run', side_effect=fake):
    assert _land() == 1
  assert merge_calls == []
  assert capsys.readouterr().out == ''


def test_land_merges_a_pr_on_a_base_that_requires_no_review():
  with _landing(_pr(reviewDecision='')) as (merge_calls, _spawned):
    assert _land() is None
  assert len(merge_calls) == 1


def test_land_refuses_a_worktree_off_the_reviewed_head(worktree_head, capsys):
  worktree_head.return_value = 'dead' * 10
  with _landing(_pr()) as (merge_calls, _spawned):
    assert _land() == 1
  assert merge_calls == []
  assert capsys.readouterr().out == ''


def test_land_refuses_a_repository_without_rebase_merging():
  with _landing(_pr(), rebase_merge='false') as (merge_calls, _spawned):
    assert _land() == 1
  assert merge_calls == []


def test_land_failed_branch_delete_degrades(capsys):
  with _landing(_pr()) as (_, spawned):
    spawned.return_value = subprocess.CompletedProcess([], 1)
    assert _land() is None
  assert json.loads(capsys.readouterr().out)['branch_deleted'] is False


class TestSplitChecks:
  def test_an_empty_rollup_splits_to_nothing(self):
    assert land_pr._split_checks([]) == ([], [])

  def test_running_check_is_pending(self):
    assert land_pr._split_checks([_check(status='IN_PROGRESS', conclusion='')]) == (['tests'], [])

  def test_queued_check_is_pending(self):
    assert land_pr._split_checks([_check(status='QUEUED', conclusion='')]) == (['tests'], [])

  def test_failed_check(self):
    assert land_pr._split_checks([_check(conclusion='FAILURE')]) == ([], ['tests'])

  def test_cancelled_and_timed_out_are_failures(self):
    entries = [_check(name='a', conclusion='CANCELLED'), _check(name='b', conclusion='TIMED_OUT')]
    assert land_pr._split_checks(entries) == ([], ['a', 'b'])

  def test_skipped_and_neutral_pass(self):
    entries = [_check(name='a', conclusion='SKIPPED'), _check(name='b', conclusion='NEUTRAL')]
    assert land_pr._split_checks(entries) == ([], [])

  def test_pending_status_context(self):
    assert land_pr._split_checks([_status_context(state='PENDING')]) == (['ci/legacy'], [])

  def test_failed_status_context(self):
    assert land_pr._split_checks([_status_context(state='FAILURE')]) == ([], ['ci/legacy'])

  def test_successful_status_context(self):
    assert land_pr._split_checks([_status_context()]) == ([], [])


class TestChecksError:
  def test_clean_rollup(self):
    assert land_pr._checks_error(310, [_check()]) is None

  def test_a_head_no_check_reported_on(self):
    error = land_pr._checks_error(310, [])
    assert error is not None and 'no status check reported' in error and '--ignore-checks' in error

  def test_failed_named(self):
    error = land_pr._checks_error(310, [_check(conclusion='FAILURE')])
    assert error is not None and 'failing checks: tests' in error

  def test_pending_named(self):
    error = land_pr._checks_error(310, [_check(status='IN_PROGRESS', conclusion='')])
    assert error is not None and 'pending checks: tests' in error

  def test_failure_wins_over_pending(self):
    entries = [
      _check(name='a', status='IN_PROGRESS', conclusion=''),
      _check(name='b', conclusion='FAILURE'),
    ]
    error = land_pr._checks_error(310, entries)
    assert error is not None and 'failing checks: b' in error


class TestAwaitChecks:
  def test_returns_at_once_when_nothing_is_pending(self):
    with patch.object(land_pr.time, 'sleep') as sleep:
      assert land_pr._await_checks(310, [_check()], 60) == [_check()]
    sleep.assert_not_called()

  def test_polls_until_the_check_concludes(self):
    pending = [_check(status='IN_PROGRESS', conclusion='')]
    with (
      patch.object(land_pr, '_pr_view', return_value={'statusCheckRollup': [_check()]}) as view,
      patch.object(land_pr.time, 'sleep') as sleep,
    ):
      assert land_pr._await_checks(310, pending, 60) == [_check()]
    sleep.assert_called_once_with(land_pr._CHECK_POLL_INTERVAL)
    view.assert_called_once_with(['statusCheckRollup'], number=310)

  def test_waits_for_a_rollup_that_is_still_empty(self):
    with (
      patch.object(land_pr, '_pr_view', return_value={'statusCheckRollup': [_check()]}),
      patch.object(land_pr.time, 'sleep') as sleep,
    ):
      assert land_pr._await_checks(310, [], 60) == [_check()]
    sleep.assert_called_once_with(land_pr._CHECK_POLL_INTERVAL)

  def test_gives_up_when_the_budget_is_spent(self):
    pending = [_check(status='IN_PROGRESS', conclusion='')]
    with patch.object(land_pr.time, 'sleep') as sleep:
      assert land_pr._await_checks(310, pending, 0) == pending
    sleep.assert_not_called()


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
  pr = _pr(statusCheckRollup=[_check(conclusion='FAILURE')])
  with _landing(pr) as (merge_calls, _spawned):
    assert _land(ignore_checks=True) is None
  assert len(merge_calls) == 1
  assert 'tests' in caplog.text


def test_ignore_checks_does_not_wait():
  pr = _pr(statusCheckRollup=[_check(status='IN_PROGRESS', conclusion='')])
  with _landing(pr) as (merge_calls, _spawned), patch.object(land_pr.time, 'sleep') as sleep:
    assert _land(ignore_checks=True, wait_checks=480) is None
  assert len(merge_calls) == 1
  sleep.assert_not_called()


def test_land_refuses_a_head_no_check_reported_on(capsys):
  merge_calls: list[list[str]] = []
  with patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr(statusCheckRollup=[]))):
    assert _land() == 1
  assert merge_calls == []
  assert capsys.readouterr().out == ''


def test_ignore_checks_merges_a_head_no_check_reported_on(caplog):
  with _landing(_pr(statusCheckRollup=[])) as (merge_calls, _spawned):
    assert _land(ignore_checks=True) is None
  assert len(merge_calls) == 1
  assert 'no status check reported on' in caplog.text


def test_land_merges_with_green_checks():
  pr = _pr(statusCheckRollup=[_check(), _status_context()])
  with _landing(pr) as (merge_calls, _spawned):
    assert _land() is None
  assert len(merge_calls) == 1


def test_land_does_not_wait_for_checks_on_a_pr_the_base_blocks():
  merge_calls: list[list[str]] = []
  pr = _pr(
    reviewDecision='REVIEW_REQUIRED',
    statusCheckRollup=[_check(status='IN_PROGRESS', conclusion='')],
  )
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr.time, 'sleep') as sleep,
  ):
    assert _land(wait_checks=480) == 1
  sleep.assert_not_called()
