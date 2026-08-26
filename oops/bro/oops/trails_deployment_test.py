import json
import os
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[2]
_SERVER = _PROJECT / 'trails' / 'server'
_EXECUTABLES = (
  _PROJECT / 'deployment' / 'app.py',
  _PROJECT / 'image_build.sh',
  _SERVER / 'bootstrap.sh',
  _SERVER / 'deploy.sh',
  _SERVER / 'run_local.sh',
  _SERVER / 'verify.sh',
  _SERVER / 'verify_image.sh',
)


def test_trails_deployment_files_are_present_and_executable():
  for path in _EXECUTABLES:
    assert path.is_file()
    assert path.stat().st_mode & os.X_OK


def test_trails_image_uses_the_shared_base_and_staged_framework_wheel():
  dockerfile = (_SERVER / 'Dockerfile').read_text()

  assert 'FROM bro-server-base\n' in dockerfile
  assert 'COPY build/bro-wheel/*.whl' in dockerfile
  assert '"$1[trails-server]"' in dockerfile
  assert 'COPY oops/trails/server/runtime_credentials.json' in dockerfile


def test_runtime_registry_resolves_store_and_tokens_from_files_then_ssm():
  registry = json.loads((_SERVER / 'runtime_credentials.json').read_text())

  assert set(registry) == {'trails', 'trails_tokens'}
  for secret in registry.values():
    file_source, ssm_source = secret['sources']
    assert set(file_source) == {'file'}
    assert ssm_source['type'] == 'ssm'


def test_scripts_use_shared_deployment_and_monitoring_assets():
  assert 'source "$(bro-oops-dir)/deploy_lib.sh"' in (_SERVER / 'deploy.sh').read_text()
  assert 'source "$(bro-oops-dir)/deploy_lib.sh"' in (_PROJECT / 'image_build.sh').read_text()
  assert '"$(bro-oops-dir)/monitor_ecs.sh"' in (_SERVER / 'verify.sh').read_text()


def test_repository_cdk_app_runs_from_the_workspace():
  cdk_config = json.loads((_PROJECT / 'deployment' / 'cdk.json').read_text())

  assert cdk_config == {'app': 'uv run python app.py'}
