import hashlib
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import credentials, log
from bro.workspace.paths import workspace_tree
from ride.repository import Repository, as_repository
from ride.runtime_bundle import RuntimeBundle
from ride.workspace import build_context
from ride.workspace.build_context import CONTAINER_DIR
from ride.workspace.metadata import read_metadata
from ride.workspace.store import _bro_tarball

_RUNTIME_IMAGE_REPOSITORY = 'bro/ride-runtime'
_RUNTIME_MOUNT = '/var/ride/runtime'
_SMOKE_TEST_TAG = 'bro/framework:smoke-test'


@dataclass(frozen=True)
class ContainerRuntime:
  image: str
  bundle_hash: str


class ContainerRuntimeResolver:
  """resolve one root's image and container materialization at most once."""

  def __init__(
    self,
    bundle: Optional[RuntimeBundle],
    repo: Optional[Repository | Path] = None,
    resolved: Optional[ContainerRuntime] = None,
  ):
    self._bundle = bundle
    self._repo = None if repo is None else as_repository(repo)
    self._resolved = resolved
    self._lock = threading.Lock()

  @classmethod
  def fixed(
    cls, runtime: ContainerRuntime, repo: Optional[Repository | Path] = None
  ) -> 'ContainerRuntimeResolver':
    return cls(None, repo, runtime)

  def resolve(self) -> ContainerRuntime:
    with self._lock:
      if self._resolved is not None:
        return self._resolved
      if self._bundle is None:
        raise RuntimeError('container runtime resolver has neither a bundle nor a resolved runtime')
      runtime_image = runtime_image_tag(self._bundle.python_version)
      _ensure_runtime_image(runtime_image, self._bundle.python_version)
      image = (
        runtime_image if self._repo is None else _ensure_project_image(runtime_image, self._repo)
      )
      self._bundle.materialize_container(runtime_image)
      self._resolved = ContainerRuntime(image=image, bundle_hash=self._bundle.hash)
      return self._resolved


@dataclass(frozen=True)
class Launch:
  """complete description of a managed container before supervision is chosen."""

  name: str
  command: list[str]
  env: Mapping[str, str]
  secrets: Collection[str]
  docker_sock: bool
  tty: bool
  forward_env: bool
  image: str
  runtime_bundle_hash: str
  optional_secrets: Collection[str] = ()
  extra_mounts: Collection[str] = ()
  repo: Optional[Repository | Path] = None


# where a container session's broker channel lands: the provisioned host socket is
# bind-mounted at this short fixed path (sun_path budget), and BROKER_CHANNEL
# carries the matching address for the entrypoint's broxy
CONTAINER_BROKER_SOCK = '/run/broker.sock'
CONTAINER_BROKER_ADDRESS = f'unix:{CONTAINER_BROKER_SOCK}'


_DOCKER_FORWARD_ENV = (
  'RIDE_COMMAND',
  'RIDE_TASK_ID',
  'GIT_AUTHOR_NAME',
  'GIT_AUTHOR_EMAIL',
  'GIT_COMMITTER_NAME',
  'GIT_COMMITTER_EMAIL',
  'BRO_LOG_LEVEL',
  'BRO_SHELL_COMMAND',
  'TERM',
  'TERM_PROGRAM',
  'TERM_PROGRAM_VERSION',
  'COLORTERM',
  'VTE_VERSION',
)


def running_mounts() -> set[str]:
  ids = subprocess.run(['docker', 'ps', '-q'], capture_output=True, text=True)
  if ids.returncode != 0 or len(ids.stdout.split()) == 0:
    return set()
  inspect = subprocess.run(
    ['docker', 'inspect', '--format', '{{range .Mounts}}{{.Source}}\n{{end}}', *ids.stdout.split()],
    capture_output=True,
    text=True,
  )
  if inspect.returncode != 0:
    return set()
  return {line for line in inspect.stdout.splitlines() if len(line) > 0}


# Ctrl+Z must stay a host-side detach event: stopping the container's foreground
# group would leave no job-control shell able to resume it.
DETACH_FLAG = '--detach-keys=ctrl-z'


def container_running(container_id: str) -> bool:
  result = subprocess.run(
    ['docker', 'inspect', '--format', '{{.State.Running}}', container_id],
    capture_output=True,
    text=True,
  )
  return result.returncode == 0 and result.stdout.strip() == 'true'


def _freezer(verb: str, container_id: str) -> None:
  result = subprocess.run(['docker', verb, container_id], capture_output=True, text=True)
  if result.returncode != 0:
    log.warning('docker %s %s failed: %s', verb, container_id, result.stderr.strip())


def suspend_until_continued(container_id: str) -> None:
  _freezer('pause', container_id)
  os.kill(0, signal.SIGTSTP)
  _freezer('unpause', container_id)


def find_container_id(session: Path) -> Optional[str]:
  """the running container whose unique workspace mount is `session`, if any."""
  if not session.is_dir():
    return None
  result = subprocess.run(
    ['docker', 'ps', '-q', '--filter', f'volume={session}'],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    return None
  ids = [line for line in result.stdout.splitlines() if len(line) > 0]
  return None if len(ids) == 0 else ids[0]


def _hash_files(inputs: list[tuple[str, Path]], seed: str = '') -> str:
  digest = hashlib.sha256(seed.encode())
  for label, path in inputs:
    if not path.is_file():
      continue
    digest.update(label.encode())
    digest.update(b'\0')
    digest.update(path.read_bytes())
  return digest.hexdigest()[:12]


def runtime_image_tag(python_version: Optional[str] = None) -> str:
  version = python_version or f'{sys.version_info.major}.{sys.version_info.minor}'
  inputs = [(name, path) for name, path in sorted(build_context.RUNTIME_FILES.items())]
  inputs.append(('project.Dockerfile', CONTAINER_DIR / 'project.Dockerfile'))
  claude_pin = CONTAINER_DIR / 'claude-code-version'
  inputs.append(('claude-code-version', claude_pin))
  return f'{_RUNTIME_IMAGE_REPOSITORY}:{_hash_files(inputs, seed=version)}'


def project_image_tag(runtime_image: str, project: Repository | Path) -> Optional[str]:
  repository = as_repository(project)
  manifests = build_context.manifest_paths(repository)
  if len(manifests) == 0:
    return None
  digest = hashlib.sha256(runtime_image.encode())
  for relative in manifests:
    content = repository.read_file(relative)
    if content is None:
      raise FileNotFoundError(f'{repository.identity} is missing manifest {relative}')
    digest.update(relative.encode())
    digest.update(b'\0')
    digest.update(content)
  return f'{repository.project_config().image_repository}:{digest.hexdigest()[:12]}'


def _image_present(tag: str) -> bool:
  return subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0


def _prune_superseded_images(current: str) -> None:
  """untag unused predecessors from the current runtime or project repository."""
  repository = current.rsplit(':', 1)[0]
  listed = subprocess.run(
    ['docker', 'images', repository, '--format', '{{.Repository}}:{{.Tag}}'],
    capture_output=True,
    text=True,
  )
  if listed.returncode != 0:
    return
  for image in listed.stdout.split():
    if image in (current, _SMOKE_TEST_TAG) or image.endswith(':<none>'):
      continue
    removed = subprocess.run(['docker', 'image', 'rm', image], capture_output=True, text=True)
    if removed.returncode == 0:
      log.info('pruned superseded image %s', image)


def build_runtime_image(tag: str, python_version: str) -> None:
  claude_version = (CONTAINER_DIR / 'claude-code-version').read_text().strip()
  log.info(
    'building runtime image %s (python %s, claude-code %s)', tag, python_version, claude_version
  )
  subprocess.run(
    [
      'docker',
      'build',
      '-t',
      tag,
      '-f',
      build_context.DOCKERFILE_PATH,
      '--build-arg',
      f'PYTHON_VERSION={python_version}',
      '--build-arg',
      f'CLAUDE_CODE_VERSION={claude_version}',
      '-',
    ],
    input=build_context.assemble_runtime(),
    check=True,
  )


def build_project_image(tag: str, runtime_image: str, project: Repository | Path) -> None:
  log.info('building project image %s from %s', tag, runtime_image)
  subprocess.run(
    [
      'docker',
      'build',
      '-t',
      tag,
      '-f',
      build_context.DOCKERFILE_PATH,
      '--build-arg',
      f'RUNTIME_IMAGE={runtime_image}',
      '-',
    ],
    input=build_context.assemble_project(project),
    check=True,
  )


def _ensure_runtime_image(tag: str, python_version: str) -> None:
  if _image_present(tag):
    log.verbose('image %s ready', tag)
    return
  build_runtime_image(tag, python_version)
  _prune_superseded_images(tag)


def _ensure_project_image(runtime_image: str, project: Repository | Path) -> str:
  tag = project_image_tag(runtime_image, project)
  if tag is None:
    return runtime_image
  if _image_present(tag):
    log.verbose('image %s ready', tag)
    return tag
  build_project_image(tag, runtime_image, project)
  _prune_superseded_images(tag)
  return tag


def _create_container(argv: list[str], store_tarball: bytes, name: str) -> str:
  """create an unstarted container and inject its in-memory scoped store."""
  created = subprocess.run(argv, capture_output=True, text=True)
  if created.returncode != 0:
    raise RuntimeError(f'docker create for {name} failed: {created.stderr.strip()}')
  container_id = created.stdout.strip()
  cp = subprocess.run(
    ['docker', 'cp', '-', f'{container_id}:/home/ride'],
    input=store_tarball,
    capture_output=True,
  )
  if cp.returncode != 0:
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)
    raise RuntimeError(
      f'docker cp of scoped store into {name} failed: {cp.stderr.decode().strip()}'
    )
  log.verbose('container %s created', container_id[:12])
  return container_id


def prepare_container(launch: Launch) -> str:
  """create the unstarted container described entirely by `launch`."""
  log.info('creating container workspace %s', launch.name)
  metadata = read_metadata(launch.name)
  repository = None if launch.repo is None else as_repository(launch.repo)
  launched_repo = None if repository is None else repository.identity
  if launched_repo != metadata.repo:
    raise ValueError(
      f'launch attachment {launched_repo or "none"} does not match workspace attachment '
      f'{metadata.repo or "none"}'
    )
  tree = workspace_tree(launch.name)
  tree.mkdir(parents=True, exist_ok=True)
  log.verbose('hydrating the scoped credential store')
  store = credentials.build_scoped_store(launch.secrets, optional=launch.optional_secrets)
  argv = _docker_create_argv(
    launch.image,
    launch.runtime_bundle_hash,
    launch.name,
    launch.repo,
    tree,
    metadata.branch,
    launch.command,
    docker_sock=launch.docker_sock,
    extra_env=dict(launch.env),
    forward_env=launch.forward_env,
    tty=launch.tty,
    extra_mounts=list(launch.extra_mounts),
  )
  return _create_container(argv, _bro_tarball(store), launch.name)


def _docker_create_argv(
  tag: str,
  runtime_bundle_hash: str,
  name: str,
  repo: Optional[Repository | Path],
  tree: Path,
  branch: Optional[str],
  command: list[str],
  *,
  docker_sock: bool = True,
  extra_env: Optional[Mapping[str, str]] = None,
  forward_env: bool = True,
  tty: bool = True,
  extra_mounts: Optional[list[str]] = None,
) -> list[str]:
  """the create half of create/copy/start, before the scoped-store injection window."""
  repository = None if repo is None else as_repository(repo)
  home = Path.home()
  argv = ['docker', 'create']
  if tty:
    argv.append('-it')
  argv += [
    '--rm',
    '--init',
    '-v',
    f'{tree}:/workspace',
    '-v',
    f'{home}/.gitconfig:/host-gitconfig:ro',
    '-v',
    f'ride-runtime-{runtime_bundle_hash}:{_RUNTIME_MOUNT}:ro',
    '-e',
    'HOME=/home/ride',
    '-e',
    f'RIDE_WORKSPACE={name}',
    '-e',
    f'RIDE_HOST_WORKSPACE={tree}',
    '-e',
    f'RIDE_HOST={socket.gethostname()}',
    '-w',
    '/workspace',
    '--memory=8g',
  ]
  if repository is not None:
    if branch is None:
      raise ValueError('attached container workspace has no recorded branch')
    argv += [
      '-v',
      f'{repository.git_dir}:/host-repo:ro',
      '-e',
      f'RIDE_REPO={repository.identity}',
      '-e',
      f'RIDE_BRANCH={branch}',
    ]
  # The socket gives the container host-daemon control, so the scoped launch must
  # opt in explicitly rather than inheriting it with the platform image.
  if docker_sock:
    argv += ['-v', '/var/run/docker.sock:/var/run/docker.sock']
  # Summoned children pass a complete explicit snapshot and disable ambient
  # forwarding so the parent's task and identity facts cannot leak into them.
  if forward_env:
    for variable in _DOCKER_FORWARD_ENV:
      if os.environ.get(variable) is not None:
        argv += ['-e', variable]
  if extra_mounts is not None:
    for mount in extra_mounts:
      argv += ['-v', mount]
  if extra_env is not None:
    for key, value in extra_env.items():
      argv += ['-e', f'{key}={value}']
  return [*argv, tag, *command]
