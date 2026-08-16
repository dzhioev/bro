"""the Harbor environment that keeps a trial's files inside the task container.

Harbor's docker environment bind-mounts each trial's `/logs` directories from
the path its own process resolves, and reads the agent logs, the artifacts and
the verifier's reward off the host side of them. That path names the same
directory to the docker daemon only while harbor runs on the docker host;
driven from a container against a mounted socket it does not, and a trial that
ran to completion and was graded ends as `RewardFileNotFoundError`.

`EnvironmentCapabilities.mounted` is harbor's own answer, the one its remote
providers run under: false turns each of those reads into a `docker compose cp`,
addressed to the container. Withholding the bind mounts as well is what keeps a
trial's files off the docker host entirely.
"""

import contextlib
from collections.abc import Generator
from pathlib import Path, PurePosixPath
from typing import override

from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.trial.config import ServiceVolumeConfig
from harbor.models.trial.paths import EnvironmentPaths


def trial_mount(mount: ServiceVolumeConfig) -> bool:
  """whether `mount` is one of the trial directories harbor manages itself."""
  if mount.get('type') != 'bind':
    return False
  target = PurePosixPath(str(mount.get('target', '')))
  return target.is_relative_to(EnvironmentPaths.logs_dir)


class UnmountedDockerEnvironment(DockerEnvironment):
  """a docker environment whose trial directories live in the container."""

  @property
  @override
  def capabilities(self) -> EnvironmentCapabilities:
    return super().capabilities.model_copy(update={'mounted': False})

  @override
  def _write_mounts_compose_file(self) -> Path:
    with self._trial_mounts_withheld():
      return super()._write_mounts_compose_file()

  @override
  async def start(self, force_build: bool) -> None:
    await super().start(force_build)
    await self.ensure_dirs(self._mount_targets(writable_only=True))

  @contextlib.contextmanager
  def _trial_mounts_withheld(self) -> Generator[None]:
    """the declared mounts without the trial directories, for the duration.

    The full list remains the environment's own: it is where the directories to
    create in the container are read from, and harbor derives the task's
    `ENV_*_PATH` / `HOST_*_PATH` variables from it too.
    """
    declared = self._mounts
    self._mounts = [mount for mount in declared if not trial_mount(mount)]
    try:
      yield
    finally:
      self._mounts = declared
