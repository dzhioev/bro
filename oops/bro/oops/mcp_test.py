import importlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.llm.mcp import Context
from bro.mcp import mount
from bro.oops import mcp
from bro.oops.targets import (
  PLAN_UNSAFE_EXIT_CODE,
  Command,
  DeployTarget,
  ECSService,
  HeaderAuth,
  HTTPProbe,
  SSMParameterAuth,
  TargetRegistry,
  load_project_registry,
)
from bros.devoops import Devoops

_ROOT = Path(__file__).resolve().parents[3]


def _state(tmp_path: Path, target: DeployTarget) -> mcp.OperationsState:
  registry = TargetRegistry(load_targets=lambda: {'service': target}, needed_secrets=())
  return mcp.OperationsState(root=tmp_path, registry=registry)


def _command(tmp_path: Path, name: str) -> Command:
  path = tmp_path / name
  path.write_text('#!/usr/bin/env bash\n')
  path.chmod(0o755)
  return Command(name)


def test_repository_registry_declares_trails_server_from_infra_config(monkeypatch):
  from oops import deploy_targets

  config = SimpleNamespace(
    region='example-region',
    delegated_subdomain='services.example.com',
    platform=SimpleNamespace(cluster_name='example-cluster'),
    trails=SimpleNamespace(service_name='example-service'),
  )
  monkeypatch.setattr('bro.oops.cdk.config.resolve', lambda: config)

  target = deploy_targets.registry.targets()['trails-server']

  assert target.deploy == Command('oops/trails/server/deploy.sh')
  assert target.verify == Command('oops/trails/server/verify.sh')
  assert target.plan is not None
  assert mcp._command(_ROOT, target.plan)
  assert target.ecs == ECSService('example-region', 'example-cluster', 'example-service')
  assert target.probe == HTTPProbe('https://trails.services.example.com/health')
  assert deploy_targets.registry.needed_secrets == ('aws', 'github', 'infra')


def test_toolset_manifest_follows_the_project_registry():
  registry = load_project_registry(_ROOT)
  assert registry is not None
  assert mount(mcp.toolset).server_specs[0].needed_secrets == registry.needed_secrets


def test_roster_reports_a_repository_relative_command_and_a_null_plan(tmp_path):
  target = DeployTarget(deploy=Command('oops/trails/server/deploy.sh', ('--flag',)))

  roster = json.loads(mcp.list_targets(Context(_state(tmp_path, target))))

  assert roster['service']['deploy'] == 'oops/trails/server/deploy.sh --flag'
  assert roster['service']['plan'] is None


def test_commands_cannot_escape_the_repository(tmp_path):
  target = DeployTarget(deploy=_command(tmp_path, 'deploy.sh'), verify=Command('../verify.sh'))

  with pytest.raises(ValueError, match='escapes the repository'):
    mcp.verify(Context(_state(tmp_path, target)), 'service')


def test_streaming_command_timeout_kills_the_process_group(tmp_path):
  result = mcp._run_streaming(['sleep', '60'], timeout_seconds=1, cwd=tmp_path)

  assert result['timed_out'] is True
  assert result['exit_code'] != 0


def test_streaming_command_prepends_the_serving_environment(tmp_path):
  result = mcp._run_streaming(['bash', '-c', 'printf %s "$PATH"'], 10, tmp_path)

  assert result['exit_code'] == 0
  assert result['output'].startswith(f'{Path(sys.executable).parent}:')


def test_verify_runs_the_declared_command(tmp_path, monkeypatch):
  target = DeployTarget(
    deploy=_command(tmp_path, 'deploy.sh'),
    verify=_command(tmp_path, 'verify.sh'),
  )
  calls = []
  monkeypatch.setattr(
    mcp,
    '_run_streaming',
    lambda command, timeout_seconds, cwd: (
      calls.append((command, cwd)) or {'command': 'verify', 'exit_code': 0, 'output': 'healthy'}
    ),
  )

  result = json.loads(mcp.verify(Context(_state(tmp_path, target)), 'service'))

  assert result['ok'] is True
  assert calls == [([f'{tmp_path}/verify.sh'], tmp_path)]


def test_plan_runs_the_declared_command(tmp_path, monkeypatch):
  target = DeployTarget(deploy=_command(tmp_path, 'deploy.sh'), plan=_command(tmp_path, 'plan.sh'))
  calls = []
  monkeypatch.setattr(
    mcp,
    '_run_streaming',
    lambda command, timeout_seconds, cwd: (
      calls.append((command, cwd)) or {'command': 'plan', 'exit_code': 0, 'output': 'no changes'}
    ),
  )

  result = json.loads(mcp.plan(Context(_state(tmp_path, target)), 'service'))

  assert (result['outcome'], result['ok']) == ('clean', True)
  assert calls == [([f'{tmp_path}/plan.sh'], tmp_path)]


def test_plan_separates_an_unsafe_change_from_one_it_never_judged(tmp_path, monkeypatch):
  target = DeployTarget(deploy=_command(tmp_path, 'deploy.sh'), plan=_command(tmp_path, 'plan.sh'))
  exit_codes = iter((PLAN_UNSAFE_EXIT_CODE, 1))
  monkeypatch.setattr(
    mcp,
    '_run_streaming',
    lambda command, timeout_seconds, cwd: {
      'command': 'plan',
      'exit_code': next(exit_codes),
      'output': '',
    },
  )
  context = Context(_state(tmp_path, target))

  unsafe = json.loads(mcp.plan(context, 'service'))
  never_judged = json.loads(mcp.plan(context, 'service'))

  assert (unsafe['outcome'], unsafe['ok']) == ('unsafe', False)
  assert (never_judged['outcome'], never_judged['ok']) == ('failed', False)


def test_plan_is_refused_for_a_target_that_declares_none(tmp_path):
  target = DeployTarget(deploy=_command(tmp_path, 'deploy.sh'))

  with pytest.raises(ValueError, match='declares no plan command'):
    mcp.plan(Context(_state(tmp_path, target)), 'service')


def test_restart_uses_the_target_ecs_coordinates(tmp_path, monkeypatch):
  target = DeployTarget(
    deploy=_command(tmp_path, 'deploy.sh'),
    verify=_command(tmp_path, 'verify.sh'),
    ecs=ECSService('example-region', 'example-cluster', 'example-service'),
  )
  commands = []

  def run_aws(command, timeout_seconds):
    commands.append(command)
    return {'exit_code': 0, 'stdout': 'deployment-id', 'stderr': ''}

  monkeypatch.setattr(mcp, '_run_aws', run_aws)
  monkeypatch.setattr(
    mcp,
    '_run_streaming',
    lambda command, timeout_seconds, cwd: {'command': 'verify', 'exit_code': 0, 'output': ''},
  )

  result = json.loads(mcp.restart(Context(_state(tmp_path, target)), 'service', dry_run=False))

  assert result['deployment_id'] == 'deployment-id'
  assert commands[0][commands[0].index('--cluster') + 1] == 'example-cluster'
  assert commands[0][commands[0].index('--service') + 1] == 'example-service'
  assert commands[0][commands[0].index('--region') + 1] == 'example-region'


def test_probe_uses_a_declared_header_without_exposing_it_in_target_list(tmp_path, monkeypatch):
  target = DeployTarget(
    deploy=_command(tmp_path, 'deploy.sh'),
    probe=HTTPProbe(
      'https://service.example.com/health',
      HeaderAuth('Authorization', 'Bearer secret-token'),
    ),
  )
  commands = []

  def run(command, **kwargs):
    commands.append(command)
    return subprocess.CompletedProcess(command, 0, stdout='200', stderr='')

  monkeypatch.setattr(mcp.spawn, 'run', run)
  context = Context(_state(tmp_path, target))

  assert json.loads(mcp.probe(context, 'service'))['http_code'] == '200'
  assert 'Authorization: Bearer secret-token' in commands[0]
  listed = json.loads(mcp.list_targets(context))
  assert listed['service']['probe']['auth'] == 'header'
  assert 'secret-token' not in json.dumps(listed)


def test_probe_resolves_a_declared_ssm_parameter_header(tmp_path, monkeypatch):
  target = DeployTarget(
    deploy=_command(tmp_path, 'deploy.sh'),
    probe=HTTPProbe(
      'https://service.example.com/health',
      SSMParameterAuth(
        header='Authorization',
        prefix='Bearer ',
        parameter='/service/token',
        region='example-region',
      ),
    ),
  )
  commands = []

  def run(command, **kwargs):
    commands.append(command)
    stdout = 'stored-token' if command[0] == 'aws' else '200'
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')

  monkeypatch.setattr(mcp.spawn, 'run', run)

  result = json.loads(mcp.probe(Context(_state(tmp_path, target)), 'service'))

  assert result['http_code'] == '200'
  assert commands[0][:3] == ['aws', 'ssm', 'get-parameter']
  assert 'Authorization: Bearer stored-token' in commands[1]


def test_registry_rejects_invalid_target_names():
  registry = TargetRegistry(
    load_targets=lambda: {'Not Valid': DeployTarget(Command('deploy.sh'))},
    needed_secrets=(),
  )

  with pytest.raises(ValueError, match='invalid deploy target name'):
    registry.targets()


def test_devoops_manifest_comes_from_its_components(monkeypatch):
  bro_module = importlib.import_module('bro.bro')
  monkeypatch.setattr(bro_module.credentials, 'known_names', lambda: {'brog'})
  monkeypatch.setattr(bro_module.credentials, 'available', lambda name: name == 'brog')

  persona = Devoops()

  assert persona.needed_secrets() == ('aws', 'brog', 'github', 'infra')
  assert persona.needed_secrets(harness='claude') == ('aws', 'brog', 'github', 'infra')
  assert persona.extra_secrets == ()


def test_devoops_is_registered_as_a_persona():
  entries = importlib.metadata.entry_points(group='bro', name=Devoops.name)
  assert [entry.load() for entry in entries] == [Devoops]
