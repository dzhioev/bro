import json
from unittest.mock import patch

import pytest

from bro.workflow import base_ref, check_head, head_checks
from bro.workflow.git_test_helper import repository as repository


def _run(head, conclusion='success', status='completed'):
  return {
    'databaseId': 42,
    'headSha': head,
    'status': status,
    'conclusion': conclusion,
    'url': 'https://github.com/owner/project/actions/runs/42',
  }


@pytest.mark.parametrize('conclusion', ['success', 'failure', 'timed_out'])
def test_dispatch_result_and_cleanup(repository, conclusion, capsys):
  head = repository.git('rev-parse', 'HEAD')
  run = _run(head, conclusion, 'in_progress' if conclusion == 'timed_out' else 'completed')
  calls = []

  def gh(*arguments):
    calls.append(arguments)
    if arguments[:3] == ('gh', 'workflow', 'run'):
      assert repository.git('ls-remote', '--heads', 'origin', f'land/{head}')
      return ''
    return json.dumps([] if len(calls) == 1 else [run])

  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(head_checks, 'command', side_effect=gh),
  ):
    assert check_head.main(
      ['check-head', '--base', 'origin/main', '--workflow', 'tests.yml', '--wait', '0']
    ) == (None if conclusion == 'success' else 1)
  result = json.loads(capsys.readouterr().out)
  assert result == {'head': head, 'run': run, 'conclusion': conclusion}
  assert not repository.git('ls-remote', '--heads', 'origin', f'land/{head}')
  assert repository.git('ls-remote', '--heads', 'origin', 'main').split()[0] == repository.git(
    'rev-parse', 'main'
  )
  assert (
    'gh',
    'workflow',
    'run',
    'tests.yml',
    '--repo',
    'owner/project',
    '--ref',
    f'land/{head}',
  ) in calls


@pytest.mark.parametrize('failure', ['dispatch', 'poll'])
def test_cleanup_on_command_failure(repository, failure):
  calls = 0

  def gh(*arguments):
    nonlocal calls
    calls += 1
    if calls == (2 if failure == 'dispatch' else 3):
      raise base_ref.WorkflowError('GitHub refused')
    return '[]'

  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(head_checks, 'command', side_effect=gh),
  ):
    assert check_head.check_head('origin/main', 'tests.yml', 0) == 1
  assert not repository.git('ls-remote', '--heads', 'origin', 'land/*')


def test_waits_for_its_new_run_not_an_earlier_green_run(repository):
  head = repository.git('rev-parse', 'HEAD')
  old, new = _run(head), {**_run(head), 'databaseId': 43}
  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(
      head_checks,
      'command',
      side_effect=[json.dumps([old]), '', json.dumps([old]), json.dumps([new, old])],
    ),
    patch.object(head_checks.time, 'sleep') as sleep,
  ):
    result = head_checks.dispatch(base_ref.resolve('origin/main'), 'tests.yml', head, 60)
  assert result['run'] == new
  sleep.assert_called_once()


def test_local_base_cannot_dispatch(repository):
  with pytest.raises(base_ref.WorkflowError, match='remote base'):
    head_checks.dispatch(
      base_ref.resolve('refs/heads/main'), 'tests.yml', repository.git('rev-parse', 'HEAD'), 0
    )


@pytest.mark.parametrize(
  'url',
  [
    'git@github.com:other/project.git',
    'https://github.com/other/project.git',
    'ssh://git@github.com/other/project.git',
  ],
)
def test_repository_is_the_base_remote_not_ghs_default(repository, url):
  base = base_ref.resolve('origin/main')
  repository.git('remote', 'set-url', 'origin', url)
  assert head_checks.repository(base) == 'other/project'


def test_non_github_remote_refuses_checks(repository):
  with pytest.raises(base_ref.WorkflowError, match='GitHub remote'):
    head_checks.repository(base_ref.resolve('origin/main'))


@pytest.mark.parametrize(
  'status,conclusion,expected',
  [
    ('completed', 'success', 'success'),
    ('completed', 'failure', 'failure'),
    ('in_progress', None, 'timed_out'),
  ],
)
def test_existing_checks_are_gated_without_dispatch(repository, status, conclusion, expected):
  head = repository.git('rev-parse', 'HEAD')
  workflow = _run(head, conclusion, status)
  checks = [{'status': status, 'conclusion': conclusion}]
  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(
      head_checks,
      'command',
      side_effect=[json.dumps([workflow]), json.dumps([{'check_runs': checks}])],
    ) as command,
    patch.object(head_checks, 'dispatch') as dispatch,
  ):
    result = head_checks.ensure(base_ref.resolve('origin/main'), 'tests.yml', head, 0)
  assert result['conclusion'] == expected
  assert result['run'] == workflow
  assert f'repos/owner/project/commits/{head}/check-runs?per_page=100' in command.call_args.args
  dispatch.assert_not_called()


@pytest.mark.parametrize('conclusion', ['success', 'failure'])
def test_no_workflow_dispatches_ci_and_gates_other_checks(repository, conclusion):
  head = repository.git('rev-parse', 'HEAD')
  result = {'head': head, 'run': _run(head), 'conclusion': 'success'}
  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(
      head_checks,
      'command',
      side_effect=[
        '[]',
        json.dumps([{'check_runs': [{'status': 'completed', 'conclusion': conclusion}]}]),
      ],
    ),
    patch.object(head_checks, 'dispatch', return_value=result) as dispatch,
  ):
    base = base_ref.resolve('origin/main')
    assert head_checks.ensure(base, 'tests.yml', head, 0)['conclusion'] == conclusion
  dispatch.assert_called_once_with(base, 'tests.yml', head, 0)


@pytest.mark.parametrize('conclusion', ['success', 'failure'])
def test_green_visible_jobs_wait_for_the_workflow_to_conclude(repository, conclusion):
  head = repository.git('rev-parse', 'HEAD')
  workflows = iter([_run(head, '', 'in_progress'), _run(head, conclusion)])
  checks = json.dumps([{'check_runs': [{'status': 'completed', 'conclusion': 'success'}]}])

  def gh(*arguments):
    if arguments[:3] == ('gh', 'run', 'list'):
      return json.dumps([next(workflows)])
    assert arguments[:2] == ('gh', 'api')
    return checks

  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(head_checks, 'command', side_effect=gh),
    patch.object(head_checks.time, 'sleep') as sleep,
  ):
    result = head_checks.ensure(base_ref.resolve('origin/main'), 'tests.yml', head, 60)
  assert result['conclusion'] == conclusion
  assert result['run']['status'] == 'completed'
  sleep.assert_called_once()


def test_green_visible_jobs_do_not_clear_an_unfinished_workflow(repository):
  head = repository.git('rev-parse', 'HEAD')

  def gh(*arguments):
    if arguments[:3] == ('gh', 'run', 'list'):
      return json.dumps([_run(head, '', 'in_progress')])
    assert arguments[:2] == ('gh', 'api')
    return '[{"check_runs": [{"status": "completed", "conclusion": "success"}]}]'

  with (
    patch.object(head_checks, 'repository', return_value='owner/project'),
    patch.object(head_checks, 'command', side_effect=gh),
  ):
    result = head_checks.ensure(base_ref.resolve('origin/main'), 'tests.yml', head, 0)
  assert result['conclusion'] == 'timed_out'
