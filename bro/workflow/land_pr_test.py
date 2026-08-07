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
  }
  pr.update(overrides)
  return pr


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


_FOOTER_COMMAND = 'repo-footer'


def _project_config(footer_command: Optional[str] = _FOOTER_COMMAND):
  return type('Config', (), {'footer_command': footer_command})()


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
    if command[0] == _FOOTER_COMMAND:
      assert command[1:] == ['--squash', 'origin/master..HEAD']
      return '> created with Claude Code …'
    if command[:3] == ['gh', 'pr', 'merge']:
      merge_calls.append(command)
      return ''
    raise AssertionError(f'unexpected command: {command}')

  return run


def test_land_happy_path(capsys):
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr())),
    patch.object(land_pr, 'project_config', return_value=_project_config()),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)) as push,
  ):
    assert land_pr.land_pr(no_review=False, allow_unchecked=False) is None

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
    assert land_pr.land_pr(no_review=False, allow_unchecked=False) == 1
  assert merge_calls == []
  assert capsys.readouterr().out == ''


def test_land_failed_branch_delete_degrades(capsys):
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr())),
    patch.object(land_pr, 'project_config', return_value=_project_config()),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 1)),
  ):
    assert land_pr.land_pr(no_review=False, allow_unchecked=False) is None
  assert json.loads(capsys.readouterr().out)['branch_deleted'] is False


def test_land_without_footer_command_keeps_the_pr_body(capsys):
  merge_calls: list[list[str]] = []
  with (
    patch.object(land_pr, '_run', side_effect=_fake_run(merge_calls, _pr())),
    patch.object(land_pr, 'project_config', return_value=_project_config(None)),
    patch.object(land_pr.spawn, 'run', return_value=subprocess.CompletedProcess([], 0)),
  ):
    assert land_pr.land_pr(no_review=False, allow_unchecked=False) is None
  merge = merge_calls[0]
  assert merge[merge.index('--body') + 1] == '## Test plan\n- [x] suite green'
