import json
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.config import ServiceVolumeConfig
from harbor.models.trial.paths import TrialPaths

from bro.benchmark.harbor_environment import UnmountedDockerEnvironment

IMAGE = 'task-image:latest'
TRIAL_MOUNTS: list[ServiceVolumeConfig] = [
  {'type': 'bind', 'source': '/host/trial/agent', 'target': '/logs/agent'},
  {'type': 'bind', 'source': '/host/trial/verifier', 'target': '/logs/verifier'},
  {'type': 'bind', 'source': '/host/trial/artifacts', 'target': '/logs/artifacts'},
]
TASK_MOUNT: ServiceVolumeConfig = {
  'type': 'bind',
  'source': '/host/fixtures',
  'target': '/fixtures',
}


def environment(tmp_path: Path, *mounts: ServiceVolumeConfig) -> UnmountedDockerEnvironment:
  return UnmountedDockerEnvironment(
    environment_dir=tmp_path,
    environment_name='task',
    session_id='session',
    trial_paths=TrialPaths(tmp_path / 'trial'),
    task_env_config=EnvironmentConfig(docker_image=IMAGE),
    mounts=list(mounts),
  )


def volumes(environment: UnmountedDockerEnvironment) -> list[ServiceVolumeConfig]:
  compose = json.loads(environment._write_mounts_compose_file().read_text())
  return compose['services']['main']['volumes']


def test_harbor_reads_a_trial_through_the_container(tmp_path):
  assert not environment(tmp_path, *TRIAL_MOUNTS).capabilities.mounted


def test_harbors_own_docker_environment_reads_it_off_a_shared_filesystem(tmp_path):
  shared = DockerEnvironment(
    environment_dir=tmp_path,
    environment_name='task',
    session_id='session',
    trial_paths=TrialPaths(tmp_path / 'trial'),
    task_env_config=EnvironmentConfig(docker_image=IMAGE),
  )
  assert shared.capabilities.mounted


def test_the_trial_directories_are_not_bound_from_the_harbor_host(tmp_path):
  assert volumes(environment(tmp_path, *TRIAL_MOUNTS)) == []


def test_a_mount_declared_beyond_the_trial_is_left_alone(tmp_path):
  assert volumes(environment(tmp_path, *TRIAL_MOUNTS, TASK_MOUNT)) == [TASK_MOUNT]


def test_the_trial_directories_stay_the_environments_own(tmp_path):
  """withholding them from compose must not withhold them from the task: the
  directories to create in the container and harbor's `ENV_*_PATH` variables
  are both read off the declared mounts."""
  unmounted = environment(tmp_path, *TRIAL_MOUNTS)
  volumes(unmounted)
  assert unmounted._mount_targets() == ['/logs/agent', '/logs/verifier', '/logs/artifacts']


async def test_starting_creates_the_directories_the_mounts_would_have(tmp_path, monkeypatch):
  created: list[list[str]] = []
  monkeypatch.setattr(DockerEnvironment, 'start', _noop_start)
  unmounted = environment(tmp_path, *TRIAL_MOUNTS)
  monkeypatch.setattr(unmounted, 'ensure_dirs', lambda dirs, **kwargs: _record(created, list(dirs)))

  await unmounted.start(force_build=False)

  assert created == [['/logs/agent', '/logs/verifier', '/logs/artifacts']]


async def _noop_start(self, force_build: bool) -> None:
  return None


async def _record(created: list[list[str]], dirs: list[str]) -> None:
  created.append(dirs)
