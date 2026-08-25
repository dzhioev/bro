import json
import os
import subprocess
from pathlib import Path

_MONITOR = Path(__file__).parent / 'infra' / 'monitor_ecs.sh'


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


def test_monitor_uses_caller_supplied_coordinates_and_reports_success(tmp_path):
  responses = [
    _service([_deployment('new', 'IN_PROGRESS', running=1)]),
    _service([_deployment('new', 'COMPLETED', running=1)]),
  ]
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
[ "$*" = "ecs describe-services --cluster cluster-a --services service-a --region region-1 --query services[0].{{deployments:deployments[*].{{id:id,status:status,desired:desiredCount,running:runningCount,pending:pendingCount,failed:failedTasks,rollout:rolloutState}},events:events[:3].{{at:createdAt,msg:message}}}} --output json" ]
line="$(cat {counter_file})"
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

  result = subprocess.run(
    [str(_MONITOR), 'region-1', 'cluster-a', 'service-a'],
    capture_output=True,
    text=True,
    env=environment,
    check=False,
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout == (
    'waiting for deployment to start\n'
    'PRIMARY rollout=COMPLETED running=1/1 failed=0 | event: service event\n'
    'deploy succeeded\n'
  )
