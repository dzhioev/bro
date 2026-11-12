"""Dispatch and inspect GitHub checks for a commit."""

import contextlib
import json
import re
import time
from collections.abc import Generator
from typing import Any, Optional

from bro.extra.github.api import check_state
from bro.workflow.base_ref import Base, WorkflowError, command, git

DEFAULT_WAIT = 600
_POLL_INTERVAL = 5.0


def repository(base: Base) -> str:
  if base.remote is None:
    raise WorkflowError('GitHub checks require a remote base')
  url = git('remote', 'get-url', base.remote)
  match = re.fullmatch(
    r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?',
    url,
  )
  if match is None:
    raise WorkflowError(f'checks require a GitHub remote, not {url}')
  return match[1]


def _runs(
  repo: str, workflow: str, head: str, branch: Optional[str] = None
) -> list[dict[str, Any]]:
  arguments = [
    'gh',
    'run',
    'list',
    '--repo',
    repo,
    '--workflow',
    workflow,
    '--commit',
    head,
    '--limit',
    '100',
    '--json',
    'databaseId,headSha,status,conclusion,url',
  ]
  if branch is not None:
    arguments += ['--branch', branch, '--event', 'workflow_dispatch']
  return json.loads(command(*arguments))


@contextlib.contextmanager
def _temporary_branch(base: Base, head: str, branch: str) -> Generator[None]:
  git('push', base.destination, f'{head}:refs/heads/{branch}')
  try:
    yield
  finally:
    git('push', base.destination, '--delete', branch)


def dispatch(base: Base, workflow: str, head: str, wait: int) -> dict[str, Any]:
  repo = repository(base)
  branch = f'land/{head}'
  previous = {run['databaseId'] for run in _runs(repo, workflow, head, branch)}
  run = None
  deadline = time.monotonic() + wait
  with _temporary_branch(base, head, branch):
    command('gh', 'workflow', 'run', workflow, '--repo', repo, '--ref', branch)
    while True:
      runs = [
        entry
        for entry in _runs(repo, workflow, head, branch)
        if entry['databaseId'] not in previous
      ]
      if len(runs) > 1:
        raise WorkflowError(f'multiple dispatched runs appeared on {branch}')
      if runs:
        run = runs[0]
        if run['status'] == 'completed':
          return {'head': head, 'run': run, 'conclusion': run['conclusion']}
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        return {'head': head, 'run': run, 'conclusion': 'timed_out'}
      time.sleep(min(_POLL_INTERVAL, remaining))


def passed(result: dict[str, Any]) -> bool:
  return check_state('completed', result['conclusion']) == 'passed'


def _latest_checks(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
  latest: dict[tuple[int, str], dict[str, Any]] = {}
  for page in pages:
    for run in page['check_runs']:
      key = (run['app']['id'], run['name'])
      if key not in latest or run['id'] > latest[key]['id']:
        latest[key] = run
  return list(latest.values())


def ensure(base: Base, workflow: str, head: str, wait: int) -> dict[str, Any]:
  repo = repository(base)
  deadline = time.monotonic() + wait
  while True:
    workflows = _runs(repo, workflow, head)
    if not workflows:
      dispatched = dispatch(base, workflow, head, max(0, int(deadline - time.monotonic())))
      if not passed(dispatched):
        return dispatched
      workflow_run = dispatched['run']
    else:
      workflow_run = workflows[0]
    pages = json.loads(
      command(
        'gh',
        'api',
        '--paginate',
        '--slurp',
        f'repos/{repo}/commits/{head}/check-runs?per_page=100&filter=all',
      )
    )
    checks = _latest_checks(pages)
    states = [
      check_state(workflow_run['status'], workflow_run['conclusion']),
      *(check_state(run['status'], run['conclusion']) for run in checks),
    ]
    if 'failed' in states:
      conclusion = 'failure'
    elif 'pending' not in states:
      conclusion = 'success'
    elif time.monotonic() >= deadline:
      conclusion = 'timed_out'
    else:
      time.sleep(min(_POLL_INTERVAL, max(0, deadline - time.monotonic())))
      continue
    return {'head': head, 'run': workflow_run, 'check_runs': checks, 'conclusion': conclusion}
