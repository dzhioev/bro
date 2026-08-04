import hashlib
import os
import signal
import socket
import subprocess
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bro.base import credentials, log
from bro.workspace.paths import containers_dir, project_root
from bro.workspace.project import project_config
from bro.workspace.store import _bro_tarball

CONTAINER_DIR = Path(__file__).resolve().parent.parent / 'setup' / 'container'
BASE_IMAGE_DIR = Path(__file__).resolve().parent.parent / 'setup' / 'base_image'


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
  optional_secrets: Collection[str] = ()
  extra_mounts: Collection[str] = ()


_DOCKER_FORWARD_ENV = (
  'CW_COMMAND',
  'CW_TASK_ID',
  'GIT_AUTHOR_NAME',
  'GIT_AUTHOR_EMAIL',
  'GIT_COMMITTER_NAME',
  'GIT_COMMITTER_EMAIL',
  'BRO_LOG_LEVEL',
  'PPP_SHELL_COMMAND',
  # docker defaults containers to TERM=xterm (a low color tier that flattens
  # dim/256-color TUIs); forward the host TERM so in-container colors match.
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


# Ctrl+Z must never reach the container pty: the line discipline there would stop a
# foreground group that no job-control shell can ever resume (docker-init is the
# session leader). Binding it as the client's detach key makes it a host-side event
# instead (`suspend_until_continued`).
DETACH_FLAG = '--detach-keys=ctrl-z'


def container_running(container_id: str) -> bool:
  """whether the container currently runs (False once it exited or was removed)."""
  result = subprocess.run(
    ['docker', 'inspect', '--format', '{{.State.Running}}', container_id],
    capture_output=True,
    text=True,
  )
  return result.returncode == 0 and result.stdout.strip() == 'true'


def _freezer(verb: str, container_id: str) -> None:
  """pause/unpause, best-effort: a failure (e.g. the container exited just as the user
  detached) degrades the freeze, never the session."""
  result = subprocess.run(['docker', verb, container_id], capture_output=True, text=True)
  if result.returncode != 0:
    log.warning('docker %s %s failed: %s', verb, container_id, result.stderr.strip())


def suspend_until_continued(container_id: str) -> None:
  """host-parity Ctrl+Z for an attached session: freeze the whole container (cgroup
  freezer) and stop this process's own group, so the launching shell reports the job
  stopped; on `fg` (SIGCONT) thaw the container and return, letting the caller
  re-attach. With no job-control shell above (an orphaned process group) the kernel
  discards the self-SIGTSTP, degrading Ctrl+Z to an immediate re-attach."""
  _freezer('pause', container_id)
  os.kill(0, signal.SIGTSTP)
  _freezer('unpause', container_id)


def find_container_id(session: Path) -> Optional[str]:
  """find the running container backing the container workspace mounted at `session`.

  filters `docker ps` by the workspace's host mount path, which is unique per
  bro.workspace.returns the container short id, or None if no running container
  is bound to that mount. takes the mount path (not name+project) so this stays a
  dependency-free leaf — the caller resolves the path.
  """
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
  if len(ids) == 0:
    return None
  return ids[0]


# tagged by setup/container/test_smoke.sh, which owns its lifecycle; the smoke
# test always builds with the ppp checkout as the project context, so the
# literal repository is correct regardless of the operated repo's config
_SMOKE_TEST_TAG = 'bro/ppp-dev:smoke-test'


def image_tag() -> str:
  h = hashlib.sha256()
  project = project_root()
  inputs = (
    sorted(BASE_IMAGE_DIR.iterdir())
    + sorted(CONTAINER_DIR.iterdir())
    # ppp/pyproject.toml exists when the project vendors ppp as a submodule: the
    # baked console-script bridge is a function of its [project.scripts] table
    + [
      project / 'pyproject.toml',
      project / 'uv.lock',
      CONTAINER_DIR.parent / 'log.sh',
      project / 'ppp' / 'pyproject.toml',
    ]
  )
  for path in inputs:
    if path.is_file():
      h.update(path.name.encode())
      h.update(b'\0')
      h.update(path.read_bytes())
  return f'{project_config().image_repository}:{h.hexdigest()[:12]}'


def _prune_superseded_images(current: str) -> None:
  """untag session images of `current`'s repository superseded by it.

  every Dockerfile/manifest change mints a new content-hash tag, and the old
  image would otherwise linger forever (~2.6 GB each). plain `docker image rm`
  (no -f) refuses images that any container — running or stopped — still
  references, so live sessions keep theirs and only orphaned tags go. scoping
  the listing to `current`'s repository keeps one project's builds from
  evicting another's ([tool.bro] image-repository).
  """
  repository = current.split(':')[0]
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


def _ensure_image(tag: str) -> None:
  inspect = subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True, text=True)
  if inspect.returncode == 0:
    log.verbose('image %s ready', tag)
    return
  version = (CONTAINER_DIR / 'claude-code-version').read_text().strip()
  log.info('building %s (claude-code %s)', tag, version)
  # the image builds FROM the local-only ppp-base, so refresh that first
  subprocess.run(['bash', '-e', str(BASE_IMAGE_DIR / 'build.sh')], check=True)
  subprocess.run(
    [
      'docker',
      'build',
      '-t',
      tag,
      '-f',
      str(CONTAINER_DIR / 'Dockerfile'),
      '--build-arg',
      f'CLAUDE_CODE_VERSION={version}',
      '--build-context',
      f'project={project_root()}',
      str(CONTAINER_DIR),
    ],
    check=True,
  )
  _prune_superseded_images(tag)


def _create_container(argv: list[str], store_tarball: bytes, name: str) -> str:
  """`docker create` + `docker cp` of the scoped credential store, returning the container id.

  The run-equivalent create/start split exists for exactly this window: the store is
  injected into the pre-start container's writable layer, so no plaintext touches the
  host disk and `--rm` removes it with the container. A failed cp removes the created
  container (a created-never-started container isn't covered by `--rm`)."""
  created = subprocess.run(argv, capture_output=True, text=True)
  if created.returncode != 0:
    raise RuntimeError(f'docker create for {name} failed: {created.stderr.strip()}')
  container_id = created.stdout.strip()
  cp = subprocess.run(
    ['docker', 'cp', '-', f'{container_id}:/home/cw'],
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


def prepare_container(launch: Launch, project: Path) -> str:
  """create the workspace and unstarted container described by `launch`."""
  log.info('creating container workspace %s', launch.name)
  session = containers_dir(project) / launch.name
  session.mkdir(parents=True, exist_ok=True)
  tag = image_tag()
  _ensure_image(tag)
  log.verbose('hydrating the scoped credential store')
  store = credentials.build_scoped_store(launch.secrets, optional=launch.optional_secrets)
  argv = _docker_create_argv(
    tag,
    launch.name,
    project,
    session,
    launch.command,
    docker_sock=launch.docker_sock,
    extra_env=launch.env,
    forward_env=launch.forward_env,
    tty=launch.tty,
    extra_mounts=list(launch.extra_mounts),
  )
  return _create_container(argv, _bro_tarball(store), launch.name)


def _docker_create_argv(
  tag: str,
  name: str,
  project: Path,
  session: Path,
  command: list[str],
  *,
  docker_sock: bool = True,
  extra_env: Optional[Mapping[str, str]] = None,
  forward_env: bool = True,
  tty: bool = True,
  extra_mounts: Optional[list[str]] = None,
) -> list[str]:
  """argv for `docker create` of the session container (run-equivalent, unstarted).

  `docker create -it --rm --init …` then `docker start -a -i <id>` reproduces `docker
  run -it --rm --init` exactly (TTY, signals, exit code, auto-remove on exit). Splitting them
  gives `prepare_container` a window to `docker cp` the scoped credential store into
  the pre-start container's writable layer — no host-side store, no bind mount.

  `tty=False` is the non-TTY variant the broker's supervised children launch with: a
  headless child gets no pty — its output is captured host-side into a ring buffer, not
  rendered to a terminal. `extra_mounts` adds explicit `-v SRC:DST` bind mounts — the
  broker child mounts its provisioned host socket → the in-container `/run/broker.sock`.

  `extra_env` adds explicit `-e KEY=VALUE` entries (value set here) — distinct from the
  `_DOCKER_FORWARD_ENV` loop, which forwards a host var by name.

  `forward_env=False` switches that forward loop off entirely: a broker-spawned
  child's environment is the explicit snapshot its launcher assembled (`extra_env`)
  — forwarding the launching process's task/session identity, git author identity,
  and terminal facts would bake the launcher's values into the child
  (mis-attributed commits, wrong banner facts). `CW_BRO` is deliberately not in
  the forward set: every launch surface sets the container's bro in `extra_env`,
  so an ambient value — the calling session's own theming — never leaks in.
  """
  home = Path.home()
  argv = ['docker', 'create']
  if tty:
    argv.append('-it')
  argv += [
    '--rm',
    # tini as pid 1 reaps orphaned grandchildren. our entrypoint re-execs into
    # claude, so without this pid 1 is claude — which doesn't wait() on orphans, so
    # every group-killed pipeline (spawn.run's timeout path: the dev bro's bash/grep,
    # infra deploys) would leak a zombie grandchild for the container's lifetime.
    '--init',
    '-v',
    f'{session}:/workspace',
    '-v',
    f'{project}:/host-repo:ro',
    '-v',
    f'{home}/.gitconfig:/host-gitconfig:ro',
    '-e',
    'HOME=/home/cw',
    '-e',
    f'CW_NAME={name}',
    # surface the host-side workspace path inside the container so `cw banner`
    # can show users where their /workspace mount actually lives on the host
    '-e',
    f'CW_HOST_WORKSPACE={session}',
    # the host machine's name — a container's own gethostname is the container id
    '-e',
    f'CW_HOST={socket.gethostname()}',
    '-w',
    '/workspace',
    '--memory=8g',
  ]
  # bind-mount the host docker socket so deploy scripts inside the container can
  # `docker build` / `docker push` against the host daemon (no nested runtime).
  # gives an in-container process API-level control over host docker, a real but
  # bounded escalation vector (cw is single-user dev; the rootless-podman
  # alternative has the same blast radius across more surfaces). gated by
  # `docker_sock` so a session that does no docker work is denied it, keeping the
  # scoped boundary intact against prompt-injection exfiltration.
  if docker_sock:
    argv += ['-v', '/var/run/docker.sock:/var/run/docker.sock']
  if forward_env:
    for var in _DOCKER_FORWARD_ENV:
      if os.environ.get(var) is not None:
        argv += ['-e', var]
  if extra_mounts is not None:
    for mount in extra_mounts:
      argv += ['-v', mount]
  if extra_env is not None:
    for key, value in extra_env.items():
      argv += ['-e', f'{key}={value}']
  return [*argv, tag, *command]
