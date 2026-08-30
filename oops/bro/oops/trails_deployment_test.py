import json
import os
import subprocess
from pathlib import Path

from bro.base.spawn import console_script

_PROJECT = Path(__file__).resolve().parents[2]
_SERVER = _PROJECT / 'trails' / 'server'
_EXECUTABLES = (
  _PROJECT / 'deployment' / 'app.py',
  _PROJECT / 'image_build.sh',
  _SERVER / 'bootstrap.sh',
  _SERVER / 'deploy.sh',
  _SERVER / 'plan.sh',
  _SERVER / 'run_local.sh',
  _SERVER / 'verify.sh',
  _SERVER / 'verify_image.sh',
)


def test_trails_deployment_files_are_present_and_executable():
  for path in _EXECUTABLES:
    assert path.is_file()
    assert path.stat().st_mode & os.X_OK


def test_trails_image_uses_the_shared_base_framework_wheel_and_store():
  dockerfile = (_SERVER / 'Dockerfile').read_text()

  assert 'FROM bro-server-base\n' in dockerfile
  assert 'COPY build/bro-wheel/*.whl' in dockerfile
  assert '"$1[trails-server]"' in dockerfile
  assert 'ENV BRO_STORE=/app/.configs\n' in dockerfile
  assert 'COPY oops/trails/server/creds.json ${BRO_STORE}/creds.json' in dockerfile


def test_deployed_store_resolves_store_and_tokens_from_ssm():
  annotations = json.loads((_SERVER / 'creds.json').read_text())

  assert annotations == {
    'trails': {'type': 'ssm', 'parameter': '/trails/store-config'},
    'trails_tokens': {'type': 'ssm', 'parameter': '/trails/tokens'},
  }


def test_local_server_synthesizes_a_plain_store():
  script = (_SERVER / 'run_local.sh').read_text()

  assert '"$runtime_directory/creds/trails.cred"' in script
  assert '"$runtime_directory/creds/trails_tokens.cred"' in script
  assert 'export BRO_STORE="$runtime_directory"' in script


def test_image_verification_replaces_the_deployed_store_with_plain_material():
  script = (_SERVER / 'verify_image.sh').read_text()

  assert 'smoke_copy "$smoke_directory/creds.json" /app/.configs/creds.json' in script
  assert 'smoke_copy "$smoke_directory/creds" /app/.configs/creds' in script


def test_scripts_use_shared_deployment_and_monitoring_assets():
  assert 'source "$(bro-oops-dir)/deploy_lib.sh"' in (_SERVER / 'deploy.sh').read_text()
  assert 'source "$(bro-oops-dir)/deploy_lib.sh"' in (_PROJECT / 'image_build.sh').read_text()
  assert '"$(bro-oops-dir)/monitor_ecs.sh"' in (_SERVER / 'verify.sh').read_text()


def _run_trails_scripts(tmp_path: Path, *names: str) -> list[list[str]]:
  """drive the named server scripts against a stubbed config and deployment library,
  returning every recorded `cdk_deploy` and `cdk_diff` call as its split argument line."""
  configuration = {
    'region': 'region-1',
    'delegated_subdomain': 'services.example.com',
    'platform': {'cluster_name': 'cluster-a'},
    'repositories': {'trails': {'stack_name': 'RepositoryStack', 'repository_name': 'repo-a'}},
    'image_build': {'stack_name': 'ImageBuildStack', 'project_name': 'project-a'},
    'trails': {'stack_name': 'ServiceStack', 'repository': 'trails', 'service_name': 'service-a'},
  }
  record = tmp_path / 'record'
  (tmp_path / 'deploy_lib.sh').write_text(
    f'cdk_deploy() {{ printf "deploy %s\\n" "$*" >> {record}; }}\n'
    f'cdk_diff() {{ printf "diff %s\\n" "$*" >> {record}; }}\n'
    'trigger_image_build() { :; }\n'
  )
  stubs = tmp_path / 'bin'
  stubs.mkdir()
  (stubs / 'bro-oops-dir').write_text(f'#!/bin/sh\necho {tmp_path}\n')
  (stubs / 'python3').write_text(
    f"#!/bin/sh\ncat > /dev/null\ncat <<'JSON'\n{json.dumps(configuration)}\nJSON\n"
  )
  for stub in stubs.iterdir():
    stub.chmod(0o755)
  shell_assets = Path(console_script('bro-shell-dir')).parent
  environment = {
    **os.environ,
    'PATH': os.pathsep.join((str(stubs), str(shell_assets), os.environ['PATH'])),
  }
  for name in names:
    result = subprocess.run([str(_SERVER / name)], capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
  return [line.split() for line in record.read_text().splitlines()]


def test_the_plan_covers_every_stack_the_deploy_rolls(tmp_path):
  calls = _run_trails_scripts(tmp_path, 'deploy.sh', 'plan.sh')

  stacks = {
    operation: {stack for name, _, *stacks in calls if name == operation for stack in stacks}
    for operation in ('deploy', 'diff')
  }

  assert stacks['deploy'] == {'RepositoryStack', 'ImageBuildStack', 'ServiceStack'}
  assert stacks['diff'] == stacks['deploy']
  assert {Path(directory).resolve() for _, directory, *_ in calls} == {_PROJECT / 'deployment'}


def test_repository_cdk_app_runs_from_the_workspace():
  cdk_config = json.loads((_PROJECT / 'deployment' / 'cdk.json').read_text())

  assert cdk_config == {'app': 'uv run python app.py'}
