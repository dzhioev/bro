import contextlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from bro.base import log, spawn
from bro.llm.mcp import Context
from bro.mcp import Toolset
from bro.oops.targets import (
  Command,
  DeployTarget,
  ECSService,
  HeaderAuth,
  HTTPProbe,
  SSMParameterAuth,
  TargetRegistry,
  load_project_registry,
)
from bro.workspace.paths import project_root

_MAX_OUTPUT_LINES = 400
_AWS_TIMEOUT = 60
_PROBE_TIMEOUT = 20
_DEPLOY_TIMEOUT = 2700
_VERIFY_TIMEOUT = 900


@dataclass
class OperationsState:
  root: Path
  registry: TargetRegistry

  @cached_property
  def targets(self) -> dict[str, DeployTarget]:
    return self.registry.targets()


class _Toolset(Toolset[OperationsState]):
  def get_secrets(self, tool_names: Sequence[str]) -> tuple[str, ...]:
    registry = load_project_registry()
    return () if registry is None else registry.needed_secrets


def _operations_state() -> OperationsState:
  root = project_root()
  registry = load_project_registry(root)
  if registry is None:
    raise ValueError(
      'this repository declares no deploy targets; configure '
      '[tool.bro.devoops] target-registry in pyproject.toml'
    )
  return OperationsState(root=root, registry=registry)


toolset = _Toolset('infra', state=_operations_state)


def _target(state: OperationsState, name: str) -> DeployTarget:
  target = state.targets.get(name)
  if target is None:
    raise ValueError(f'unknown target {name!r}; known: {sorted(state.targets)}')
  return target


def _ecs(name: str, target: DeployTarget) -> ECSService:
  if target.ecs is None:
    raise ValueError(f'{name!r} declares no ECS service')
  return target.ecs


def _probe_spec(name: str, target: DeployTarget) -> HTTPProbe:
  if target.probe is None:
    raise ValueError(f'{name!r} declares no HTTP probe')
  return target.probe


def _command(root: Path, spec: Command) -> list[str]:
  root = root.resolve()
  executable = (root / spec.path).resolve()
  try:
    executable.relative_to(root)
  except ValueError as exception:
    raise ValueError(f'command escapes the repository: {spec.path!r}') from exception
  if not executable.is_file():
    raise ValueError(f'command does not exist: {spec.path!r}')
  if not os.access(executable, os.X_OK):
    raise ValueError(f'command is not executable: {spec.path!r}')
  return [str(executable), *spec.arguments]


def _timeout_result(command: object, timeout_seconds: float) -> dict[str, Any]:
  shown = ' '.join(command) if isinstance(command, list | tuple) else str(command)
  return {
    'timed_out': True,
    'command': shown,
    'exit_code': 124,
    'timeout_seconds': timeout_seconds,
    'hint': 're-run with a larger timeout_seconds',
  }


def _run_aws(command: list[str], timeout_seconds: float) -> dict[str, Any]:
  try:
    completed = spawn.run(command, capture_output=True, text=True, timeout=timeout_seconds)
  except subprocess.TimeoutExpired as exception:
    return _timeout_result(exception.cmd, exception.timeout)
  return {
    'command': ' '.join(command),
    'exit_code': completed.returncode,
    'stdout': completed.stdout.strip(),
    'stderr': completed.stderr.strip(),
  }


def _truncate(value: str, max_lines: int = _MAX_OUTPUT_LINES) -> str:
  lines = value.splitlines(keepends=True)
  if len(lines) <= max_lines:
    return value
  dropped = len(lines) - max_lines
  return f'[...{dropped} earlier lines truncated...]\n' + ''.join(lines[-max_lines:])


@contextlib.contextmanager
def _watchdog(timeout_seconds: float, on_timeout: Callable[[], None]) -> Generator[None]:
  timer = threading.Timer(timeout_seconds, on_timeout)
  timer.start()
  try:
    yield
  finally:
    timer.cancel()


def _run_streaming(command: list[str], timeout_seconds: float, cwd: Path) -> dict[str, Any]:
  environment = dict(os.environ)
  environment['PATH'] = f'{Path(sys.executable).parent}{os.pathsep}{environment["PATH"]}'
  process = spawn.popen(
    command,
    cwd=cwd,
    env=environment,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
  )
  timed_out = False

  def on_timeout() -> None:
    nonlocal timed_out
    if process.poll() is None:
      timed_out = True
      spawn.kill_group(process)

  output: list[str] = []
  with _watchdog(timeout_seconds, on_timeout):
    assert process.stdout is not None
    for line in process.stdout:
      print(line, end='', file=sys.stderr, flush=True)
      output.append(line)
    process.wait()
  result: dict[str, Any] = {
    'command': ' '.join(command),
    'exit_code': process.returncode,
    'output': _truncate(''.join(output)),
  }
  if timed_out:
    result.update(
      timed_out=True,
      timeout_seconds=timeout_seconds,
      hint='re-run with a larger timeout_seconds',
    )
  return result


def _auth_header(auth: HeaderAuth | SSMParameterAuth, timeout_seconds: float) -> dict[str, Any]:
  if isinstance(auth, HeaderAuth):
    return {'header': auth.header, 'value': auth.value}
  result = _run_aws(
    [
      'aws',
      'ssm',
      'get-parameter',
      '--region',
      auth.region,
      '--name',
      auth.parameter,
      '--with-decryption',
      '--query',
      'Parameter.Value',
      '--output',
      'text',
    ],
    timeout_seconds,
  )
  if result['exit_code'] != 0:
    return {'error': result}
  return {'header': auth.header, 'value': f'{auth.prefix}{result["stdout"]}'}


def _probe(probe: HTTPProbe, timeout_seconds: float) -> dict[str, Any]:
  headers: list[str] = []
  if probe.auth is not None:
    resolved = _auth_header(probe.auth, timeout_seconds)
    if 'error' in resolved:
      return resolved['error']
    headers = ['-H', f'{resolved["header"]}: {resolved["value"]}']
  log.info(f'probing {probe.url}')
  command = [
    'curl',
    '-sS',
    '--connect-timeout',
    '5',
    '-o',
    '/dev/null',
    '-w',
    '%{http_code}',
    *headers,
    probe.url,
  ]
  try:
    completed = spawn.run(
      command,
      capture_output=True,
      text=True,
      timeout=timeout_seconds,
    )
  except subprocess.TimeoutExpired as exception:
    return _timeout_result(exception.cmd, exception.timeout)
  return {
    'url': probe.url,
    'http_code': completed.stdout.strip(),
    'exit_code': completed.returncode,
    'stderr': completed.stderr.strip(),
  }


def _wait_and_probe(
  target_name: str,
  target: DeployTarget,
  timeout_seconds: float,
) -> dict[str, Any]:
  ecs = _ecs(target_name, target)
  probe = _probe_spec(target_name, target)
  log.info(f'waiting for {ecs.cluster}/{ecs.service} to stabilize')
  wait = _run_aws(
    [
      'aws',
      'ecs',
      'wait',
      'services-stable',
      '--cluster',
      ecs.cluster,
      '--services',
      ecs.service,
      '--region',
      ecs.region,
    ],
    timeout_seconds,
  )
  if wait['exit_code'] != 0:
    return {'stable': False, 'wait': wait, 'ok': False}
  probed = _probe(probe, _PROBE_TIMEOUT)
  return {'stable': True, 'probe': probed, 'ok': probed.get('http_code') == '200'}


def _verify(
  state: OperationsState,
  target_name: str,
  target: DeployTarget,
  timeout_seconds: float,
) -> dict[str, Any]:
  if target.verify is not None:
    result = _run_streaming(_command(state.root, target.verify), timeout_seconds, state.root)
    return {**result, 'ok': result['exit_code'] == 0}
  return _wait_and_probe(target_name, target, timeout_seconds)


def _command_summary(command: Command) -> str:
  return ' '.join((command.path, *command.arguments))


def _auth_kind(probe: HTTPProbe) -> str:
  if probe.auth is None:
    return 'none'
  if isinstance(probe.auth, HeaderAuth):
    return 'header'
  return 'ssm-parameter'


@toolset.tool(
  'list the repository-declared deploy targets with their commands, ECS service, probe, '
  'changed-path hints, and notes.'
)
def list_targets(context: Context[OperationsState]) -> str:
  result = {}
  for name, target in context.state.targets.items():
    result[name] = {
      'deploy': _command_summary(target.deploy),
      'verify': None if target.verify is None else _command_summary(target.verify),
      'ecs': (
        None
        if target.ecs is None
        else {
          'region': target.ecs.region,
          'cluster': target.ecs.cluster,
          'service': target.ecs.service,
        }
      ),
      'probe': (
        None
        if target.probe is None
        else {'url': target.probe.url, 'auth': _auth_kind(target.probe)}
      ),
      'paths': list(target.paths),
      'notes': target.notes,
    }
  return json.dumps(result, indent=2)


@toolset.tool(
  'run a repository-declared target deploy command. With dry_run=true, return the command '
  'without executing it. Call verify afterwards when the target declares verification.'
)
def deploy(
  context: Context[OperationsState],
  target: str,
  dry_run: bool,
  timeout_seconds: int = _DEPLOY_TIMEOUT,
) -> str:
  selected = _target(context.state, target)
  command = _command(context.state.root, selected.deploy)
  if dry_run:
    return json.dumps({'command': ' '.join(command), 'dry_run': True}, indent=2)
  return json.dumps(_run_streaming(command, timeout_seconds, context.state.root), indent=2)


@toolset.tool(
  'run a target verification command, or wait for its ECS service and run its HTTP probe '
  'when no command is declared. Returns ok=true only when verification succeeds.'
)
def verify(
  context: Context[OperationsState],
  target: str,
  timeout_seconds: int = _VERIFY_TIMEOUT,
) -> str:
  selected = _target(context.state, target)
  return json.dumps(_verify(context.state, target, selected, timeout_seconds), indent=2)


@toolset.tool(
  'force a new deployment of a target ECS service, then run its declared verification. '
  'Use only when the deployed image did not change, such as after rotating a runtime secret. '
  'With dry_run=true, return the AWS command without executing it.'
)
def restart(
  context: Context[OperationsState],
  target: str,
  dry_run: bool,
  timeout_seconds: int = _VERIFY_TIMEOUT,
) -> str:
  selected = _target(context.state, target)
  ecs = _ecs(target, selected)
  command = [
    'aws',
    'ecs',
    'update-service',
    '--cluster',
    ecs.cluster,
    '--service',
    ecs.service,
    '--force-new-deployment',
    '--region',
    ecs.region,
    '--query',
    'service.deployments[0].id',
    '--output',
    'text',
  ]
  if dry_run:
    return json.dumps({'command': ' '.join(command), 'dry_run': True}, indent=2)
  log.info(f'forcing new deployment for {ecs.cluster}/{ecs.service}')
  forced = _run_aws(command, _AWS_TIMEOUT)
  if forced['exit_code'] != 0:
    return json.dumps({'forced': forced, 'ok': False}, indent=2)
  verification = _verify(context.state, target, selected, timeout_seconds)
  return json.dumps(
    {
      'deployment_id': forced['stdout'],
      'forced': forced,
      'verification': verification,
      'ok': verification['ok'],
    },
    indent=2,
  )


@toolset.tool(
  'return a read-only snapshot of a target ECS service: current deployments and its five '
  'latest events.'
)
def ecs_status(
  context: Context[OperationsState],
  target: str,
  timeout_seconds: int = _AWS_TIMEOUT,
) -> str:
  selected = _target(context.state, target)
  ecs = _ecs(target, selected)
  result = _run_aws(
    [
      'aws',
      'ecs',
      'describe-services',
      '--cluster',
      ecs.cluster,
      '--services',
      ecs.service,
      '--region',
      ecs.region,
      '--query',
      'services[0].{deployments:deployments[*].{id:id,status:status,desired:desiredCount,'
      'running:runningCount,pending:pendingCount,failed:failedTasks,rollout:rolloutState},'
      'events:events[:5].{at:createdAt,msg:message}}',
      '--output',
      'json',
    ],
    timeout_seconds,
  )
  if result['exit_code'] != 0:
    return json.dumps(result, indent=2)
  return result['stdout']


@toolset.tool('run a target HTTP probe with its declarative authentication specification.')
def probe(
  context: Context[OperationsState],
  target: str,
  timeout_seconds: int = _PROBE_TIMEOUT,
) -> str:
  selected = _target(context.state, target)
  return json.dumps(_probe(_probe_spec(target, selected), timeout_seconds), indent=2)
