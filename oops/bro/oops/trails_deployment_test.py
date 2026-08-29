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


def test_repository_cdk_app_runs_from_the_workspace():
  cdk_config = json.loads((_PROJECT / 'deployment' / 'cdk.json').read_text())

  assert cdk_config == {'app': 'uv run python app.py'}
