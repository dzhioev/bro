import json
from unittest.mock import patch

import pytest

from bro.workflow import land_push
from bro.workflow.git_test_helper import repository as repository


def _land(base='origin/main', approved=None, workflow=None, delete=None):
  return land_push.land_push(base, approved, workflow, 0, [] if delete is None else delete)


@pytest.mark.parametrize('base', ['origin/main', 'refs/heads/main', 'main'])
def test_fast_forward_and_json(repository, base, capsys):
  head = repository.git('rev-parse', 'HEAD')
  if base == 'main':
    repository.git('remote', 'remove', 'origin')
  assert _land(base) is None
  landed = (
    repository.git('ls-remote', 'origin', 'main').split()[0]
    if base == 'origin/main'
    else repository.git('rev-parse', 'main')
  )
  assert landed == head
  result = json.loads(capsys.readouterr().out)
  assert result == {
    'base': base,
    'head': head,
    'commits': [head],
    'coverage': None,
    'checks': None,
    'deleted': [],
  }


@pytest.mark.parametrize('tracked', [True, False])
def test_dirty_worktree_refused(repository, tracked):
  (repository.path / ('feature' if tracked else 'untracked')).write_text('dirty\n')
  with patch.object(land_push.base_ref, 'resolve') as resolve:
    assert _land() == 1
  resolve.assert_not_called()


def _advance_base(repository):
  repository.git('checkout', '-q', 'main')
  tip = repository.commit('base-change', 'base moved\n')
  repository.git('push', '-q', 'origin', 'main')
  repository.git('checkout', '-q', 'feature')
  return tip


def test_base_moved_before_push(repository):
  tip = _advance_base(repository)
  assert _land() == land_push.BASE_MOVED
  assert repository.git('ls-remote', 'origin', 'main').split()[0] == tip


def test_base_moved_during_push(repository):
  original = land_push.spawn.run
  advanced = []

  def run(arguments, **options):
    if list(arguments[:3]) == ['git', 'push', '--porcelain']:
      tip = repository.git('commit-tree', 'main^{tree}', '-p', 'main', '-m', 'concurrent update')
      repository.git('--git-dir', str(repository.remote), 'fetch', '-q', str(repository.path), tip)
      repository.git('--git-dir', str(repository.remote), 'update-ref', 'refs/heads/main', tip)
      advanced.append(tip)
    return original(arguments, **options)

  with patch.object(land_push.spawn, 'run', side_effect=run):
    assert _land() == land_push.BASE_MOVED
  assert repository.git('ls-remote', 'origin', 'main').split()[0] == advanced[0]


def test_remote_rule_refusal_is_not_base_moved(repository, caplog):
  hook = repository.remote / 'hooks' / 'pre-receive'
  hook.write_text('#!/bin/sh\necho "direct pushes forbidden" >&2\nexit 1\n')
  hook.chmod(0o755)
  assert _land() == 1
  assert 'direct pushes forbidden' in caplog.text


def test_exact_approved_head_and_remote_cleanup(repository, capsys):
  head = repository.git('rev-parse', 'HEAD')
  repository.git('push', '-q', 'origin', 'HEAD:review/topic')
  assert _land(approved=head, delete=['review/topic']) is None
  result = json.loads(capsys.readouterr().out)
  assert result['coverage'] == 'head'
  assert result['deleted'] == ['review/topic']
  assert not repository.git('ls-remote', '--heads', 'origin', 'review/topic')


def test_unchanged_rebase_is_covered(repository, capsys):
  approved = repository.git('rev-parse', 'HEAD')
  _advance_base(repository)
  repository.git('rebase', 'origin/main')
  assert repository.git('rev-parse', 'HEAD') != approved
  assert _land(approved=approved) is None
  assert json.loads(capsys.readouterr().out)['coverage'] == 'range-diff'


@pytest.mark.parametrize('change', ['amend', 'append'])
def test_changed_commits_are_not_covered(repository, change):
  approved = repository.git('rev-parse', 'HEAD')
  if change == 'amend':
    (repository.path / 'feature').write_text('changed\n')
    repository.git('add', 'feature')
    repository.git('commit', '-qm', 'changed commit', '--amend')
  else:
    repository.commit('extra', 'extra\n')
  assert _land(approved=approved) == 1
  assert repository.git('ls-remote', 'origin', 'main').split()[0] == repository.git(
    'rev-parse', 'main'
  )


def test_no_approved_head_does_not_require_coverage(repository):
  repository.commit('extra', 'extra\n')
  with patch.object(land_push.base_ref, 'git', wraps=land_push.base_ref.git) as git:
    assert _land() is None
  assert all(call.args[0] != 'range-diff' for call in git.call_args_list)


@pytest.mark.parametrize('conclusion', ['success', 'failure', 'timed_out'])
def test_workflow_gate_is_on_head_before_push(repository, conclusion, capsys):
  head = repository.git('rev-parse', 'HEAD')
  checks = {'head': head, 'run': {'id': 42}, 'conclusion': conclusion}
  with patch.object(land_push.head_checks, 'ensure', return_value=checks) as ensure:
    assert _land(workflow='tests.yml') == (None if conclusion == 'success' else 1)
  assert ensure.call_args.args[1:] == ('tests.yml', head, 0)
  remote_head = repository.git('ls-remote', 'origin', 'main').split()[0]
  if conclusion == 'success':
    assert remote_head == head
    assert json.loads(capsys.readouterr().out)['checks'] == checks
  else:
    assert remote_head != head
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
  'base,delete', [('refs/heads/main', ['review']), ('origin/main', ['main'])]
)
def test_invalid_deletion_refused_before_landing(repository, base, delete):
  assert _land(base=base, delete=delete) == 1
  assert repository.git('ls-remote', 'origin', 'main').split()[0] != repository.git(
    'rev-parse', 'HEAD'
  )


@pytest.mark.parametrize('name', ['refs/heads/integration/topic', 'refs/tags/release'])
def test_cleanup_cannot_escape_its_validated_branch_namespace(repository, name):
  head = repository.git('rev-parse', 'HEAD')
  repository.git('push', '-q', 'origin', 'main:integration/topic', 'main:refs/tags/release')
  base = 'origin/integration/topic'
  assert _land(base, delete=[name]) == 1
  assert repository.git('ls-remote', 'origin', 'integration/topic').split()[0] == repository.git(
    'rev-parse', 'main'
  )
  assert repository.git('ls-remote', 'origin', 'refs/tags/release')
  assert repository.git('ls-remote', 'origin', 'integration/topic').split()[0] != head


def test_merge_only_content_is_not_an_unchanged_rebase(repository):
  approved = repository.git('rev-parse', 'HEAD')
  base = _advance_base(repository)
  repository.git('merge', '--no-ff', '--no-commit', 'origin/main')
  (repository.path / 'unreviewed').write_text('not reviewed\n')
  repository.git('add', 'unreviewed')
  repository.git('commit', '-qm', 'merge with unreviewed content')
  assert _land(approved=approved) == 1
  assert repository.git('ls-remote', 'origin', 'main').split()[0] == base


def test_conflict_resolution_requires_a_successors_approved_head(repository, capsys):
  approved = repository.git('rev-parse', 'HEAD')
  repository.git('checkout', '-q', 'main')
  base = repository.commit('feature', 'conflicting base content\n')
  repository.git('push', '-q', 'origin', 'main')
  repository.git('checkout', '-q', 'feature')
  assert _land(approved=approved) == land_push.BASE_MOVED
  rebase = land_push.spawn.run(['git', 'rebase', 'origin/main'], capture_output=True, text=True)
  assert rebase.returncode != 0
  assert repository.git('diff', '--name-only', '--diff-filter=U') == 'feature'
  (repository.path / 'feature').write_text('reviewed integration resolution\n')
  repository.git('add', 'feature')
  repository.git('-c', 'core.editor=true', 'rebase', '--continue')
  head = repository.git('rev-parse', 'HEAD')
  assert _land(approved=approved) == 1
  assert repository.git('ls-remote', 'origin', 'main').split()[0] == base
  assert _land(approved=head) is None
  result = json.loads(capsys.readouterr().out)
  assert result['coverage'] == 'head'
  assert result['head'] == head
  assert repository.git('ls-remote', 'origin', 'main').split()[0] == head
