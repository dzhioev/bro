import hashlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from bro.base import credentials, log
from bro.monitor import PROCESS_FILENAME
from bro.workspace.paths import runtime_base, workspace_tree
from ride.repository import Repository, as_repository
from ride.runtime_bundle import RuntimeBundle
from ride.workspace import build_context
from ride.workspace.build_context import CONTAINER_DIR
from ride.workspace.clones import ensure_clone
from ride.workspace.metadata import read_metadata
from ride.workspace.store import store_tarball

_RUNTIME_IMAGE_REPOSITORY = 'bro/ride-runtime'
_RUNTIME_MOUNT = '/var/ride/runtime'
_SMOKE_TEST_TAG = 'bro/framework:smoke-test'
_PREFLIGHT_MOUNT = '/var/ride/daemon-preflight'


def _daemon_name() -> str:
  result = subprocess.run(['docker', 'context', 'show'], capture_output=True, text=True)
  return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else 'default'


def _preflight_daemon(runtime_image: str) -> None:
  """Verify that the selected daemon sees the launcher's runtime root."""
  root = runtime_base()
  root.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(prefix='.daemon-preflight-', dir=root) as directory:
    nonce = os.urandom(24).hex()
    path = Path(directory) / 'nonce'
    path.write_text(nonce)
    inside = f'{_PREFLIGHT_MOUNT}/{Path(directory).name}/nonce'
    result = subprocess.run(
      [
        'docker',
        'run',
        '--rm',
        '--entrypoint',
        'cat',
        '-v',
        f'{root}:{_PREFLIGHT_MOUNT}:ro',
        runtime_image,
        inside,
      ],
      capture_output=True,
      text=True,
    )
    if result.returncode != 0 or result.stdout != nonce:
      detail = result.stderr.strip() or 'the mounted nonce was not visible'
      raise RuntimeError(
        f'Docker daemon {_daemon_name()!r} cannot bind-mount the ride runtime root {root}: {detail}'
      )


def _assert_bind_source(source: Path) -> None:
  root = runtime_base().resolve()
  try:
    source.resolve().relative_to(root)
  except ValueError as error:
    raise ValueError(
      f'container bind source lies outside the ride runtime root {root}: {source}'
    ) from error


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
    self._preflighted = resolved is not None
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
      if not self._preflighted:
        _preflight_daemon(runtime_image)
        self._preflighted = True
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
  tty: bool
  forward_env: bool
  image: str
  runtime_bundle_hash: str
  optional_secrets: Collection[str] = ()
  credential_selection: Mapping[str, str] = field(default_factory=dict)
  extra_mounts: Collection[str] = ()
  repo: Optional[Repository | Path] = None
  base_ref: Optional[str] = None


# How a container reaches its session's broker upstream:
# Docker maps this name to the gateway back to the launching host.
CONTAINER_BROKER_HOST = 'host.docker.internal'


def bridge_gateway() -> Optional[str]:
  """the address a container on the default bridge reaches its host at, as the
  daemon reports it, or None when it names none. A daemon in a VM (Docker
  Desktop, colima) names a gateway inside that VM, which is no address of this
  host — the caller that tries to bind it is what tells the two apart."""
  try:
    result = subprocess.run(
      ['docker', 'network', 'inspect', 'bridge', '--format', '{{(index .IPAM.Config 0).Gateway}}'],
      capture_output=True,
      text=True,
    )
  except OSError as error:  # no daemon to launch containers with, so none to reach back
    log.verbose('no docker bridge gateway (%s)', error)
    return None
  if result.returncode != 0:
    log.verbose('no docker bridge gateway (%s)', result.stderr.strip())
    return None
  return result.stdout.strip() or None


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
  """the host paths mounted into running containers. raises when docker cannot
  answer — a caller deciding workspace liveness off this set must not mistake an
  unreachable daemon for no active sessions."""
  ids = subprocess.run(['docker', 'ps', '-q'], capture_output=True, text=True)
  if ids.returncode != 0:
    raise RuntimeError(f'docker ps failed: {ids.stderr.strip()}')
  if len(ids.stdout.split()) == 0:
    return set()
  inspect = subprocess.run(
    ['docker', 'inspect', '--format', '{{range .Mounts}}{{.Source}}\n{{end}}', *ids.stdout.split()],
    capture_output=True,
    text=True,
  )
  if inspect.returncode != 0:
    raise RuntimeError(f'docker inspect failed: {inspect.stderr.strip()}')
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
  log.info('creating boxed workspace %s', launch.name)
  metadata = read_metadata(launch.name)
  repository = None if launch.repo is None else as_repository(launch.repo)
  launched_repo = None if repository is None else repository.identity
  if launched_repo != metadata.repo:
    raise ValueError(
      f'launch attachment {launched_repo or "none"} does not match workspace attachment '
      f'{metadata.repo or "none"}'
    )
  tree = workspace_tree(launch.name)
  if repository is None:
    if launch.base_ref is not None:
      raise ValueError('detached container launch has a base ref')
    tree.mkdir(parents=True, exist_ok=True)
  else:
    if metadata.branch is None:
      raise ValueError('attached boxed workspace has no recorded branch')
    ensure_clone(repository, tree, metadata.branch, launch.base_ref)
  log.verbose('hydrating the scoped credential store')
  source_store = credentials.Store(
    credentials.default_registry(), credentials.STORE_DIR, launch.credential_selection
  )
  store, hydrated_kinds = credentials.build_scoped_store(
    source_store, launch.secrets, optional=launch.optional_secrets
  )
  launch_env = {
    **launch.env,
    'BRO_STORE': '/home/ride/.bro',
    'BRO_INSTALL_KINDS': ' '.join(sorted(hydrated_kinds)),
  }
  argv = _docker_create_argv(
    launch.image,
    launch.runtime_bundle_hash,
    launch.name,
    launch.repo,
    tree,
    launch.command,
    extra_env=launch_env,
    forward_env=launch.forward_env,
    tty=launch.tty,
    extra_mounts=list(launch.extra_mounts),
  )
  return _create_container(argv, store_tarball(store, PurePosixPath('.bro')), launch.name)


# where a joined member's scoped store and hook files live inside the party's
# container — the container-layer sibling of the root session's /home/ride/.bro,
# one subdirectory per member
CONTAINER_MEMBER_ROOT = PurePosixPath('/home/ride/.bro-party')

# what an `env -i` member exec starts from: the runtime image's own PATH
# (ride/setup/container/Dockerfile) plus the fixed session home and a headless
# terminal baseline
MEMBER_BASELINE_ENV = {
  'HOME': '/home/ride',
  'PATH': '/var/ride/runtime/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin',
  'TERM': 'dumb',
  'LANG': 'C.UTF-8',
}

# runs ahead of the member's session command inside the party's container: the
# `docker exec` client's death does not end the process inside, so the member
# records its pid and start time first thing, before do-ride's own slower path
# to the same record — a kill can then find its target from the moment the
# process exists. The content matches do-ride's byte for byte, so do-ride's
# exit-time ownership check still removes the file.
_MEMBER_WRAPPER = f"""set -e
start=$(sed 's/.*) //' "/proc/$$/stat" | cut -d ' ' -f 20)
printf '{{"pid": %d, "start_time": "linux-ticks:%s"}}' "$$" "$start" > "$RIDE_SESSION_DIR/{PROCESS_FILENAME}.tmp"
mv "$RIDE_SESSION_DIR/{PROCESS_FILENAME}.tmp" "$RIDE_SESSION_DIR/{PROCESS_FILENAME}"
exec "$@"
"""

# signals a member only while /proc still shows the recorded start time, so a
# reused pid is never a target. KILL falls back from the member's process group
# to the process alone when the member leads no group.
_MEMBER_KILL = """actual=$(sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d ' ' -f 20)
[ "$actual" = "$2" ] || exit 0
if [ "$3" = KILL ]; then kill -s KILL -- "-$1" 2>/dev/null || kill -s KILL "$1"; else kill -s "$3" "$1"; fi
"""


def member_store_dir(member: str) -> PurePosixPath:
  return CONTAINER_MEMBER_ROOT / member / 'store'


def member_install_dir(member: str) -> PurePosixPath:
  return CONTAINER_MEMBER_ROOT / member / 'environment'


@dataclass(frozen=True)
class MemberExec:
  """complete description of a party member exec'd into its boxed party's
  running container, before supervision is chosen."""

  container: str
  member: str
  command: list[str]
  env: Mapping[str, str]
  secrets: Collection[str]
  optional_secrets: Collection[str] = ()
  credential_selection: Mapping[str, str] = field(default_factory=dict)


def member_exec_argv(launch: MemberExec, env: Mapping[str, str]) -> list[str]:
  """the `docker exec` client argv for one member: `env -i` with the explicit
  snapshot, because an exec otherwise inherits the container's config env — the
  party's first session's entire launch environment."""
  pairs = [f'{key}={value}' for key, value in sorted(env.items())]
  return [
    'docker',
    'exec',
    '-i',
    '-u',
    'ride',
    '-w',
    '/workspace',
    launch.container,
    'env',
    '-i',
    *pairs,
    'sh',
    '-c',
    _MEMBER_WRAPPER,
    'ride-member',
    *launch.command,
  ]


def member_kill_argv(container: str, pid: int, start_ticks: str, signal_name: str) -> list[str]:
  return [
    'docker',
    'exec',
    container,
    'sh',
    '-c',
    _MEMBER_KILL,
    'sh',
    str(pid),
    start_ticks,
    signal_name,
  ]


def prepare_member_exec(launch: MemberExec) -> list[str]:
  """deliver the member's scoped store into the running container and return the
  exec argv: hydrate the store, `docker cp` it to the member's own layer path,
  and re-own it as the entrypoint re-owns the root's (a no-op on Linux, where
  `ride` is already remapped to the host uid; required on Docker for Mac)."""
  log.verbose('hydrating the scoped credential store for member %s', launch.member)
  source_store = credentials.Store(
    credentials.default_registry(), credentials.STORE_DIR, launch.credential_selection
  )
  store, hydrated_kinds = credentials.build_scoped_store(
    source_store, launch.secrets, optional=launch.optional_secrets
  )
  store_root = member_store_dir(launch.member).relative_to(CONTAINER_MEMBER_ROOT.parent)
  tarball = store_tarball(store, store_root)
  cp = subprocess.run(
    ['docker', 'cp', '-', f'{launch.container}:{CONTAINER_MEMBER_ROOT.parent}'],
    input=tarball,
    capture_output=True,
  )
  if cp.returncode != 0:
    raise RuntimeError(
      f'docker cp of the scoped store for member {launch.member} failed: '
      f'{cp.stderr.decode().strip()}'
    )
  chown = subprocess.run(
    ['docker', 'exec', '-u', 'root', launch.container]
    + ['chown', '-R', 'ride:ride', str(CONTAINER_MEMBER_ROOT)],
    capture_output=True,
    text=True,
  )
  if chown.returncode != 0:
    raise RuntimeError(
      f'cannot re-own the scoped store for member {launch.member}: {chown.stderr.strip()}'
    )
  env = {
    **launch.env,
    'BRO_STORE': str(member_store_dir(launch.member)),
    'BRO_INSTALL_KINDS': ' '.join(sorted(hydrated_kinds)),
  }
  return member_exec_argv(launch, env)


def _docker_create_argv(
  tag: str,
  runtime_bundle_hash: str,
  name: str,
  repo: Optional[Repository | Path],
  tree: Path,
  command: list[str],
  *,
  extra_env: Optional[Mapping[str, str]] = None,
  forward_env: bool = True,
  tty: bool = True,
  extra_mounts: Optional[list[str]] = None,
) -> list[str]:
  """The create half of create/copy/start, before scoped-store injection."""
  repository = None if repo is None else as_repository(repo)
  _assert_bind_source(tree)
  for mount in extra_mounts or []:
    _assert_bind_source(Path(mount.split(':', 1)[0]))
  argv = ['docker', 'create']
  if tty:
    argv.append('-it')
  argv += [
    '--rm',
    '--init',
    '-v',
    f'{tree}:/workspace',
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
    # a daemon on this host resolves it to the bridge gateway; one in a VM
    # already serves the name and answers with its own proxy to host loopback
    '--add-host',
    f'{CONTAINER_BROKER_HOST}:host-gateway',
  ]
  if repository is not None:
    argv += ['-e', f'RIDE_REPO={repository.identity}']
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
