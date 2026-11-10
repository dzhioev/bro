from unittest.mock import patch

import pytest

from bro.workflow import base_ref
from bro.workflow.git_test_helper import repository as repository


def test_remote_base_is_fetched_even_without_a_tracking_ref(repository):
  repository.git('remote', 'rename', 'origin', 'upstream')
  repository.git('update-ref', '-d', 'refs/remotes/upstream/main')
  resolved = base_ref.resolve('upstream/main')
  assert resolved.remote == 'upstream'
  assert resolved.branch == 'main'
  assert resolved.tip == repository.git('rev-parse', 'main')
  assert repository.git('rev-parse', 'refs/remotes/upstream/main') == resolved.tip


@pytest.mark.parametrize('remotes', [True, False])
def test_explicit_local_branch_is_never_fetched(repository, remotes):
  if not remotes:
    repository.git('remote', 'remove', 'origin')
  with patch.object(base_ref.spawn, 'run', wraps=base_ref.spawn.run) as run:
    resolved = base_ref.resolve('refs/heads/main')
  assert resolved.remote is None
  assert resolved.tip == repository.git('rev-parse', 'main')
  assert all('fetch' not in call.args[0] for call in run.call_args_list)


def test_unqualified_local_branch_without_remotes(repository):
  repository.git('remote', 'remove', 'origin')
  assert base_ref.resolve('main').tip == repository.git('rev-parse', 'main')


def test_unqualified_base_names_every_remote_that_has_it(repository):
  repository.git('remote', 'add', 'upstream', str(repository.remote))
  repository.git('branch', 'integration/topic', 'main')
  repository.git('push', '-q', 'origin', 'integration/topic')
  repository.git('branch', '-D', 'integration/topic')
  for name in ('main', 'integration/topic'):
    with pytest.raises(base_ref.WorkflowError) as error:
      base_ref.resolve(name)
    assert f'origin/{name}' in str(error.value)
    assert f'upstream/{name}' in str(error.value)
    assert f'refs/heads/{name}' in str(error.value)


@pytest.mark.parametrize('name', ['absent', 'refs/heads/absent'])
def test_missing_ref_refuses_with_explicit_local_form(repository, name):
  with pytest.raises(base_ref.WorkflowError, match='refs/heads/absent'):
    base_ref.resolve(name)


def test_missing_remote_branch_does_not_use_a_stale_tracking_ref(repository):
  repository.git('update-ref', 'refs/remotes/origin/absent', 'HEAD')
  with pytest.raises(base_ref.WorkflowError):
    base_ref.resolve('origin/absent')


def test_default_uses_origins_live_default(repository):
  repository.git('branch', 'trunk', 'main')
  repository.git('push', '-q', 'origin', 'trunk')
  repository.git('--git-dir', str(repository.remote), 'symbolic-ref', 'HEAD', 'refs/heads/trunk')
  assert base_ref.resolve(None).ref == 'origin/trunk'


def test_default_needs_origin_even_with_other_remotes(repository):
  repository.git('remote', 'rename', 'origin', 'upstream')
  with pytest.raises(base_ref.WorkflowError, match='omitted base needs origin'):
    base_ref.resolve(None)
  repository.git('remote', 'remove', 'upstream')
  with pytest.raises(base_ref.WorkflowError, match='omitted base needs origin'):
    base_ref.resolve(None)


def test_explicit_local_ref_is_local_even_with_a_remote_named_refs(repository):
  repository.git('remote', 'add', 'refs', str(repository.remote))
  assert base_ref.resolve('refs/heads/main').remote is None
