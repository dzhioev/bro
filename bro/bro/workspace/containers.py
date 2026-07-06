import json
import os
import subprocess
import sys
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Optional

from base import credentials, log
from cw.docker import (
  _create_container,
  _docker_create_argv,
  _ensure_image,
  _image_tag,
  find_container_id,
)
from cw.paths import _containers_dir, _project_root
from cw.secrets import _ppp_tarball
from cw.workspace import ContainerWorkspace, _format_ref, _parse_ref

# explicit container-side ~/.claude.json config (installMethod matches the
# image's npm-global claude; the project entry pre-accepts the trust dialog).
_CONTAINER_CLAUDE_JSON: dict = {
  'installMethod': 'global',
  'autoUpdates': False,
  'hasCompletedOnboarding': True,
  # the pyright-lsp plugin + official marketplace are baked into the image and
  # seeded by the entrypoint; mark the auto-install done so claude doesn't re-run
  # the network fetch (and never prompts) at session start.
  'officialMarketplaceAutoInstallAttempted': True,
  'officialMarketplaceAutoInstalled': True,
  'projects': {'/workspace': {'hasTrustDialogAccepted': True}},
}
# account-identity keys carried over from the host so the session starts logged
# in (the OAuth bearer itself arrives via CLAUDE_CODE_OAUTH_TOKEN; these hold the
# matching account metadata claude renders the logged-in account from).
_CLAUDE_JSON_IDENTITY_KEYS = ('oauthAccount', 'userID')


def _seed_container_claude_json(claude_dir: Path, host_file: Path) -> Path:
  """seed-once per-workspace container-private ~/.claude.json.

  built from the explicit container config plus the host's account-identity
  fields — no host machine state copied. missing identity is fatal. subsequent
  runs keep whatever the container last wrote.
  """
  seed = claude_dir / '.claude.json'
  if not seed.exists():
    if not host_file.is_file():
      raise SystemExit(f'missing {host_file} — log in with claude on the host first')
    host = json.loads(host_file.read_text())
    data = dict(_CONTAINER_CLAUDE_JSON)
    for key in _CLAUDE_JSON_IDENTITY_KEYS:
      if key not in host:
        raise SystemExit(f'{host_file} has no {key!r} — log in with claude on the host first')
      data[key] = host[key]
    seed.write_text(json.dumps(data))
    seed.chmod(0o600)
  return seed


def exec_in_workspace(name: str, command: list[str]) -> int:
  """exec a command in the running container backing the named workspace.

  with no command, starts an interactive bash. either way, `/workspace/.venv`
  is sourced first so the workspace's console scripts (created by `uv sync`)
  are on PATH; the prompt's `(.venv)` prefix is dropped after `.bashrc` re-runs,
  but VIRTUAL_ENV and PATH survive.
  """
  name, _ = _parse_ref(name)
  project = _project_root()
  container_id = find_container_id(_containers_dir(project) / name)
  if container_id is None:
    log.error('no running container for workspace %r', name)
    return 1
  if len(command) == 0:
    docker_command = ['bash', '-c', 'source /workspace/.venv/bin/activate 2>/dev/null; exec bash']
  else:
    docker_command = [
      'bash',
      '-c',
      'source /workspace/.venv/bin/activate 2>/dev/null; exec "$@"',
      'cw-exec',
      *command,
    ]
  # run as cw, not the image's default root: docker exec ignores the entrypoint's
  # gosu drop, so without -u every exec'd command runs as root and writes
  # root-owned files into the bind-mounted /workspace that the host user can't
  # later remove. the entrypoint remaps cw to the host uid, so -u cw matches the
  # session user and keeps workspace files host-owned.
  return subprocess.run(
    ['docker', 'exec', '-it', '-u', 'cw', container_id, *docker_command]
  ).returncode


def _broker_enabled() -> bool:
  """whether this launch runs under the broker (a channel for every session, host
  and container alike).

  `BROKER_DISABLED` is the presence-checked kill-switch (parallel to `TRAILS_DISABLED`):
  the broker sits on the critical launch path of every session, so a broker defect
  needs an escape valve that works without touching code. It is checked before
  any broker import, and an unimportable broker package (an environment provisioned
  before broker existed) degrades to the broker-less path with a warning — the gate
  itself can never break a launch.
  """
  if os.environ.get('BROKER_DISABLED') is not None:
    return False
  try:
    import broker  # noqa: F401
  except ImportError:
    log.warning('broker package not importable; launching without a broker channel')
    return False
  return True


def _container_broker_enabled() -> bool:
  """`_broker_enabled`, plus the container flavor's docker-daemon constraint.

  the container's channel is the host socket bind-mounted at `/run/broker.sock`,
  which requires a docker daemon that shares the host filesystem. on macOS the
  daemon runs in a VM (Docker Desktop / colima) whose file sharing cannot project
  a host unix socket: the mount is unappliable — which breaks container creation
  outright, because the scoped store's `docker cp` stats its destination by
  mounting the whole container filesystem — and even a mounted socket file could
  not carry connections across the VM boundary. container sessions there run
  broker-less; the host flavor keeps its channel (its socket is reached
  in-process, no daemon in between).
  """
  if sys.platform == 'darwin':
    log.info('no broker channel: the macOS docker daemon cannot bind-mount host unix sockets')
    return False
  return _broker_enabled()


def _run_root_via_broker(
  name: str,
  command: list[str],
  project: Path,
  *,
  secrets: Collection[str],
  optional_secrets: Collection[str],
  docker_sock: bool,
  extra_env: Optional[Mapping[str, str]],
  forward_bro: bool,
  may_summon: Collection[str],
) -> int:
  """run the session as the broker's root peer: provision its channel socket under
  `var/cw/broker`, bind-mount it at `/run/broker.sock`, launch attached, and supervise
  it on the broker loop until it exits. Returns the container's exit code."""
  # imported here, not at module level: _broker_enabled() must be able to short-circuit
  # a launch before anything touches the broker package (see its docstring).
  from cw.spawn import DockerLaunchSpec, run_root_via_broker
  from cw.summon import STATUS_ENV, container_status_path

  # the summon session key is the mode-prefixed workspace name — a same-name host
  # session must not share the state files (see cw/summon.py)
  session = _format_ref(name, True)
  env = dict(extra_env) if extra_env is not None else {}
  # the summon-status file the host-side SummonControl writes, as seen through
  # the container's read-only /host-repo mount of the project root
  env[STATUS_ENV] = container_status_path(project, session)
  launch = DockerLaunchSpec(
    command=command,
    env=env,
    secrets=secrets,
    attached=True,
    name=name,
    optional_secrets=optional_secrets,
    docker_sock=docker_sock,
    forward_bro=forward_bro,
  )
  return run_root_via_broker(launch, project, session=session, may_summon=may_summon)


def run_in_container(
  name: str,
  command: list[str],
  *,
  drop: bool = False,
  secrets: Collection[str] = (),
  optional_secrets: Collection[str] = (),
  docker_sock: bool = True,
  extra_env: Optional[Mapping[str, str]] = None,
  forward_bro: bool = True,
  may_summon: Collection[str] = (),
) -> int:
  """run `command` inside a fresh cw-style container backed by workspace `name`.

  builds/reuses the image, creates `var/cw/containers/<name>/`, runs the container
  (`docker create` + `docker cp` scoped secrets in + `docker start -a -i`, the
  run-equivalent split that lets us inject the store into the pre-start container)
  with the standard bind mounts (`/workspace`, `/host-repo:ro`, `.claude` overlay,
  optionally the docker socket, …). When `drop=True`, removes the workspace dir
  and per-session claude state on exit. Returns the container's exit code.

  Unless the gate degrades it (see `_container_broker_enabled`: `BROKER_DISABLED`,
  an unimportable broker, or a macOS host), the session runs as the root peer of a
  broker whose loop supervises it: the per-peer channel socket is provisioned before
  `docker create`, bind-mounted at `/run/broker.sock`, and pointed at by
  `BROKER_CHANNEL`. The post-exit finish below runs after `Broker.run()` returns.

  `secrets` is the required scoped credential set hydrated into the container's
  ~/.ppp (see `credentials.build_scoped_store`); a missing secret raises (strict).
  `optional_secrets` is the best-effort tier — hydrated when resolvable, silently
  skipped when not, so a component that uses a secret only when present (e.g. a
  query-focused fetch summary) degrades instead of failing launch. AWS is
  just one of the required ones (`aws`), wired in by its install hook. `docker_sock=False`
  drops the docker socket mount (shell-less bros). `extra_env` sets explicit
  `-e KEY=VALUE` vars in the container (see `_docker_create_argv`). `forward_bro=False`
  keeps the calling session's ambient `CW_BRO` out of the container — used by the
  `ask`/`do-task`/`call` hop, whose container runs its own named bro, so the calling
  session's theming must not leak in (see `_docker_create_argv`). `may_summon` is
  the session's outgoing summon allow-list — the bro names it may summon, computed
  per `cw/summon.py` — handed to the broker root; defaults to deny-all (and is
  moot on the broker-less fallback path, which has no channel to summon over).
  """
  # the container starts with origin/master only as fresh as the host's last fetch
  # (the entrypoint copies the ref from /host-repo, no network). that is fine: the
  # only operations that decide on master ancestry — /pr and /land — fetch from
  # GitHub themselves before rebasing (the container's origin points upstream), so
  # a launch-time refresh here would buy nothing they don't redo. the lone reader of
  # a possibly-stale ref, infra's git_changes diff, is informational.
  project = _project_root()
  names = sorted(set(secrets))
  log.info('scoped secrets for %s: %s', name, ', '.join(names) if len(names) > 0 else '(none)')
  optional_names = sorted(set(optional_secrets) - set(secrets))
  if len(optional_names) > 0:
    log.info('optional (best-effort) secrets for %s: %s', name, ', '.join(optional_names))
  if _container_broker_enabled():
    code = _run_root_via_broker(
      name,
      command,
      project,
      secrets=secrets,
      optional_secrets=optional_secrets,
      docker_sock=docker_sock,
      extra_env=extra_env,
      forward_bro=forward_bro,
      may_summon=may_summon,
    )
  else:
    session = _containers_dir(project) / name
    session.mkdir(parents=True, exist_ok=True)
    tag = _image_tag()
    _ensure_image(tag)
    # build the scoped store in memory (strict: a missing secret raises before the
    # container is created), then inject it into the pre-start container's writable
    # layer via `docker cp`. nothing plaintext touches the host disk.
    store = credentials.build_scoped_store(secrets, optional=optional_secrets)
    argv = _docker_create_argv(
      tag,
      name,
      project,
      session,
      command,
      docker_sock=docker_sock,
      extra_env=extra_env,
      forward_bro=forward_bro,
    )
    container_id = _create_container(argv, _ppp_tarball(store), name)
    # `docker start -a -i` reattaches the TTY/stdin and returns the exit code; --rm
    # (set at create) removes the container — and its scoped secrets — on exit.
    code = subprocess.run(['docker', 'start', '-a', '-i', container_id]).returncode
  if drop:
    try:
      ContainerWorkspace(name, project).remove()
      log.info('removed container workspace %s', name)
    except RuntimeError as e:
      log.warning('could not fully remove container workspace %s: %s', name, e)
  return code
