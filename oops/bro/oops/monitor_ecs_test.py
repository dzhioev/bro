import json
import os
import subprocess
from pathlib import Path

_MONITOR = Path(__file__).parent / 'infra' / 'monitor_ecs.sh'

_QUERY = (
  'services[0].{deployments:deployments[*].{id:id,status:status,desired:desiredCount,'
  'running:runningCount,pending:pendingCount,failed:failedTasks,rollout:rolloutState},'
  'events:events[:3].{at:createdAt,msg:message}}'
)


def _service(deployments):
  return {'deployments': deployments, 'events': [{'msg': 'service event'}]}


def _deployment(identifier: str, rollout: str, *, running: int, status: str = 'PRIMARY'):
  return {
    'id': identifier,
    'status': status,
    'desired': 1,
    'running': running,
    'pending': 0,
    'failed': 0,
    'rollout': rollout,
  }


def _run_monitor(tmp_path: Path, responses, *arguments: str) -> subprocess.CompletedProcess:
  """drive the monitor against a stub `aws` replaying `responses` one call at a time.

  The last response sticks, so a test states the sequence of service states it cares
  about rather than counting how many times the monitor polls.
  """
  response_file = tmp_path / 'responses'
  response_file.write_text('\n'.join(json.dumps(response) for response in responses) + '\n')
  counter_file = tmp_path / 'counter'
  counter_file.write_text('1\n')
  executable_directory = tmp_path / 'bin'
  executable_directory.mkdir()
  aws = executable_directory / 'aws'
  aws.write_text(
    f"""#!/bin/sh
set -eu
[ "$*" = "ecs describe-services --cluster cluster-a --services service-a --region region-1 --query {_QUERY} --output json" ]
line="$(cat {counter_file})"
total="$(wc -l < {response_file})"
[ "$line" -gt "$total" ] && line="$total"
sed -n "${{line}}p" {response_file}
echo "$((line + 1))" > {counter_file}
"""
  )
  aws.chmod(0o755)
  environment = {
    **os.environ,
    'PATH': f'{executable_directory}:{os.environ["PATH"]}',
    'POLL_INTERVAL': '0',
  }

  # the monitor polls until it reaches a terminal state, so a regression that never
  # reaches one hangs rather than fails: the timeout turns that back into a failure.
  return subprocess.run(
    [str(_MONITOR), 'region-1', 'cluster-a', 'service-a', *arguments],
    capture_output=True,
    text=True,
    env=environment,
    check=False,
    timeout=30,
  )


def test_monitor_uses_caller_supplied_coordinates_and_reports_success(tmp_path):
  responses = [
    _service([_deployment('new', 'IN_PROGRESS', running=1)]),
    _service([_deployment('new', 'COMPLETED', running=1)]),
  ]

  result = _run_monitor(tmp_path, responses)

  assert result.returncode == 0, result.stderr
  assert result.stdout == (
    'waiting for deployment to start\n'
    'PRIMARY rollout=COMPLETED running=1/1 failed=0 | event: service event\n'
    'deploy succeeded\n'
  )


def test_monitor_accepts_a_service_that_already_settled(tmp_path):
  # a deploy command that waits for its own rollout leaves nothing in progress by the
  # time verification runs; without this the start-wait loop polls forever.
  responses = [_service([_deployment('new', 'COMPLETED', running=1)])]

  result = _run_monitor(tmp_path, responses)

  assert result.returncode == 0, result.stderr
  assert result.stdout.endswith('deploy succeeded\n')


def test_monitor_still_waits_for_a_named_old_deployment_to_be_replaced(tmp_path):
  # given the deployment id to replace, a settled service is the *old* one: keep waiting.
  responses = [
    _service([_deployment('old', 'COMPLETED', running=1)]),
    _service([_deployment('new', 'COMPLETED', running=1)]),
  ]

  result = _run_monitor(tmp_path, responses, 'old')

  assert result.returncode == 0, result.stderr
  assert result.stdout.endswith('deploy succeeded\n')


def test_monitor_reports_a_failed_rollout(tmp_path):
  responses = [_service([_deployment('new', 'FAILED', running=0)])]

  result = _run_monitor(tmp_path, responses)

  assert result.returncode == 1
  assert 'deploy failed' in result.stderr
