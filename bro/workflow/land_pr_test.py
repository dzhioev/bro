#!/usr/bin/env python
import json
import subprocess
from typing import Any, Optional
from unittest.mock import patch

import bro.workflow.land_pr as land_pr


def _pr(**overrides: Any) -> dict[str, Any]:
  pr: dict[str, Any] = {
    'number': 310,
    'title': 'cw: land in one shot',
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


def _fake_run(
  merge_calls: list[list[str]],
  pr: dict[str, Any],
  footer: str = '> created with Claude Code …',
):
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
    if command[0] == 'commit-footer':
      assert command[1:] == ['--squash', 'origin/master..HEAD']
      return footer
    if command[:3] == ['gh', 'pr', 'merge']:
      merge_calls.append(command)
      return ''
    raise AssertionError(f'unexpected command: {command}')

  return run


def test_land_happy_path(capsys):
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr())),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)) as push,
  ):
    assert _land() is None

  assert len(merge_calls) == 1
  merge = merge_calls[0]
  assert merge[:5] == ['gh', 'pr', 'merge', '310', '--squash']
  assert merge[merge.index('--subject') + 1] == 'cw: land in one shot'
  expected_body = '## Test plan\n- [x] suite green\n\n> created with Claude Code …'
  assert merge[merge.index('--body') + 1] == expected_body
  push.assert_called_once_with(['git', 'push', 'origin', '--delete', 'worktree-feature'], text=True)

  assert json.loads(capsys.readouterr().out) == {
    'pr': 310,
    'url': 'https://github.com/o/r/pull/310',
    'title': 'cw: land in one shot',
    'base': 'master',
    'squash_sha': 'abc123',
    'merged_at': '2026-07-03T10:41:02Z',
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
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr())),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 1)),
  ):
    assert _land() is None
  assert json.loads(capsys.readouterr().out)['branch_deleted'] is False


def test_land_with_an_unaccounted_branch_keeps_the_pr_body(capsys):
  # commit-footer --squash prints nothing for a branch with no footered commits
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr(), footer='')),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)),
  ):
    assert _land() is None
  merge = merge_calls[0]
  assert merge[merge.index('--body') + 1] == '## Test plan\n- [x] suite green'


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
  merge_calls: list[list[str]] = []
  pr = _pr(statusCheckRollup=[_check(conclusion='FAILURE'), _check('lint', status='QUEUED')])
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)),
  ):
    assert _land(ignore_checks=True) is None
  assert len(merge_calls) == 1
  assert 'tests' in caplog.text and 'lint' in caplog.text


def test_ignore_checks_does_not_wait():
  merge_calls: list[list[str]] = []
  pr = _pr(statusCheckRollup=[_check(status='IN_PROGRESS', conclusion='')])
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)),
    patch.object(land_pr.time, 'sleep') as sleep,
  ):
    assert _land(ignore_checks=True, wait_checks=480) is None
  sleep.assert_not_called()


def test_land_merges_with_green_checks(capsys):
  merge_calls: list[list[str]] = []
  pr = _pr(statusCheckRollup=[_check(), _status_context()])
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, pr)),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)),
  ):
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
