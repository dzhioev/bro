import hashlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from base import log
from cw.claude_config import _seed_claude_json, _write_session_settings
from cw.paths import _project_root, _session_claude_dir

CONTAINER_DIR = Path(__file__).resolve().parent.parent / 'setup' / 'container'
BASE_IMAGE_DIR = Path(__file__).resolve().parent.parent / 'setup' / 'base_image'

_DOCKER_FORWARD_ENV = (
  'CW_COMMAND',
  'CW_TASK_ID',
  'GIT_AUTHOR_NAME',
  'GIT_AUTHOR_EMAIL',
  'GIT_COMMITTER_NAME',
  'GIT_COMMITTER_EMAIL',
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


def find_container_id(session: Path) -> Optional[str]:
  """find the running container backing the container workspace mounted at `session`.

  filters `docker ps` by the workspace's host mount path, which is unique per
  workspace. returns the container short id, or None if no running container
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


_IMAGE_REPOSITORY = 'ppp-cw'
# tagged by setup/container/test_smoke.sh, which owns its lifecycle
_SMOKE_TEST_TAG = f'{_IMAGE_REPOSITORY}:smoke-test'


def _image_tag() -> str:
  h = hashlib.sha256()
  project = _project_root()
  inputs = (
    sorted(BASE_IMAGE_DIR.iterdir())
    + sorted(CONTAINER_DIR.iterdir())
    + [project / 'pyproject.toml', project / 'uv.lock']
  )
  for path in inputs:
    if path.is_file():
      h.update(path.name.encode())
      h.update(b'\0')
      h.update(path.read_bytes())
  return f'{_IMAGE_REPOSITORY}:{h.hexdigest()[:12]}'


def _prune_superseded_images(current: str) -> None:
  """untag `ppp-cw` images superseded by the just-built `current`.

  every Dockerfile/manifest change mints a new content-hash tag, and the old
  image would otherwise linger forever (~2.6 GB each). plain `docker image rm`
  (no -f) refuses images that any container — running or stopped — still
  references, so live sessions keep theirs and only orphaned tags go.
  """
  listed = subprocess.run(
    ['docker', 'images', _IMAGE_REPOSITORY, '--format', '{{.Repository}}:{{.Tag}}'],
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
    return
  version = (CONTAINER_DIR / 'claude-code-version').read_text().strip()
  log.info('building %s (claude-code %s)', tag, version)
  # the image builds FROM the local-only ppp-base, so refresh that first
  subprocess.run([str(BASE_IMAGE_DIR / 'build.sh')], check=True)
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
      f'project={_project_root()}',
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
  return container_id


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
  gives `run_in_container` a window to `docker cp` the scoped credential store into
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
  claude_dir = _session_claude_dir(name)
  claude_dir.mkdir(parents=True, exist_ok=True)
  # seed-once container-private ~/.claude.json: installMethod matches the image's
  # npm-global claude; the trusted project entry is the clone's mount point
  claude_json = _seed_claude_json(
    claude_dir, home / '.claude.json', install_method='global', trusted_paths=['/workspace']
  )
  _write_session_settings(claude_dir, container=True)
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
    f'{claude_json}:/home/cw/.claude.json',
    '-v',
    f'{claude_dir}:/home/cw/.claude',
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
    '-e',
    'DISABLE_AUTOUPDATER=1',
    # doctor would otherwise flag the absent host-native ~/.local/bin/claude
    '-e',
    'DISABLE_INSTALLATION_CHECKS=1',
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
