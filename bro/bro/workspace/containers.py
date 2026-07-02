import json
import os
import subprocess
import sys
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Optional

from base import credentials, log
from cw.docker import _docker_create_argv, _ensure_image, _image_tag, find_container_id
from cw.paths import _containers_dir, _latest_jsonl, _project_root
from cw.secrets import _ppp_tarball
from cw.workspace import ContainerWorkspace, _parse_ref

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
  proj = _project_root()
  container_id = find_container_id(_containers_dir(proj) / name)
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


def _replace_container_resume_hint(name: str) -> None:
  """overwrite claude's misleading `claude --resume <id>` hint with a host-side one.

  claude prints a two-line resume hint on exit, but the `claude --resume <id>`
  command it suggests only works inside the container — the session jsonl
  lives at ~/.claude/cw-sessions/<name>/projects/-workspace/ on the host, not
  where a bare host-side `claude` would look. We replace it with the cw-side
  resume command that actually works, carrying this session's own flags
  (CW_RESUME_COMMAND, set by start_session) so it reproduces the session.

  Only meaningful when stdout is a TTY (otherwise the ANSI escape is junk in
  a log) and a session jsonl exists (otherwise claude didn't print a hint).
  """
  if not sys.stdout.isatty():
    return
  if _latest_jsonl(ContainerWorkspace(name, _project_root()).claude_projects_dir()) is None:
    return
  resume_command = os.environ.get('CW_RESUME_COMMAND', f'cw ss -c --resume {name}')
  # \033[2A: move cursor up 2 lines (over claude's hint).
  # \033[J:  clear from cursor to end of screen.
  sys.stdout.write('\033[2A\033[J')
  print('Resume this session with:')
  print(f'  {resume_command}')


def _sync_container_log(name: str, proj: Path) -> None:
  """upload a finished `--bro` container session's transcript from the host.

  `claude --bare` (the `--bro` flow) runs hooks-free, so the in-container
  `sync-session-log` SessionStart/SessionEnd hooks never fire and the session
  would never reach S3/DynamoDB. cw owns the lifecycle and the transcript is
  bind-mounted to the host at the workspace's `claude_projects_dir`, so it does
  the one-shot upload itself after the container exits (and flushed the jsonl).
  best-effort: a sync failure warns rather than failing session teardown.
  """
  import sync_session_log

  projects_dir = ContainerWorkspace(name, proj).claude_projects_dir()
  if _latest_jsonl(projects_dir) is None:
    return
  try:
    sync_session_log.sync_session_log(workspace=name, projects_dir=projects_dir)
  except Exception as e:
    log.warning('host-side session-log sync for %s failed: %s', name, e)


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
) -> int:
  """run `command` inside a fresh cw-style container backed by workspace `name`.

  builds/reuses the image, creates `var/cw/containers/<name>/`, runs the container
  (`docker create` + `docker cp` scoped secrets in + `docker start -a -i`, the
  run-equivalent split that lets us inject the store into the pre-start container)
  with the standard bind mounts (`/workspace`, `/host-repo:ro`, `.claude` overlay,
  optionally the docker socket, …). When `drop=True`, removes the workspace dir
  and per-session claude state on exit. Returns the container's exit code.

  `secrets` is the required scoped credential set hydrated into the container's
  ~/.ppp (see `credentials.build_scoped_store`); a missing secret raises (strict).
  `optional_secrets` is the best-effort tier — hydrated when resolvable, silently
  skipped when not, so a component that uses a secret only when present (e.g. a
  query-focused fetch summary) degrades instead of failing launch. AWS is
  just one of the required ones (`aws`), wired in by its install hook. `docker_sock=False`
  drops the docker socket mount (shell-less bros). `extra_env` sets explicit
  `-e KEY=VALUE` vars in the container (see `_docker_create_argv`). `forward_bro=False`
  keeps the calling session's ambient `CW_BRO` out of the container — used by the
  `ask`/`do-task`/`call` hop, whose LLM-process container never runs Claude Code and
  so must not trigger a `cw populate-bro-skills` (see `_docker_create_argv`).
  """
  proj = _project_root()
  session = _containers_dir(proj) / name
  session.mkdir(parents=True, exist_ok=True)
  # the container starts with origin/master only as fresh as the host's last fetch
  # (the entrypoint copies the ref from /host-repo, no network). that is fine: the
  # only operations that decide on master ancestry — /pr and /land — fetch from
  # GitHub themselves before rebasing (the container's origin points upstream), so
  # a launch-time refresh here would buy nothing they don't redo. the lone reader of
  # a possibly-stale ref, infra's git_changes diff, is informational.
  tag = _image_tag()
  _ensure_image(tag)
  # build the scoped store in memory (strict: a missing secret raises before the
  # container is created), then inject it into the pre-start container's writable
  # layer via `docker cp`. nothing plaintext touches the host disk.
  store = credentials.build_scoped_store(secrets, optional=optional_secrets)
  names = sorted(set(secrets))
  log.info('scoped secrets for %s: %s', name, ', '.join(names) if len(names) > 0 else '(none)')
  optional_names = sorted(set(optional_secrets) - set(secrets))
  if len(optional_names) > 0:
    log.info('optional (best-effort) secrets for %s: %s', name, ', '.join(optional_names))
  created = subprocess.run(
    _docker_create_argv(
      tag,
      name,
      proj,
      session,
      command,
      docker_sock=docker_sock,
      extra_env=extra_env,
      forward_bro=forward_bro,
    ),
    capture_output=True,
    text=True,
  )
  if created.returncode != 0:
    raise RuntimeError(f'docker create for {name} failed: {created.stderr.strip()}')
  container_id = created.stdout.strip()
  cp = subprocess.run(
    ['docker', 'cp', '-', f'{container_id}:/home/cw'],
    input=_ppp_tarball(store),
    capture_output=True,
  )
  if cp.returncode != 0:
    # a created-never-started container isn't covered by --rm; remove it so it
    # doesn't linger (cw clean would reclaim it anyway, but eagerly is tidier).
    subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True)
    raise RuntimeError(
      f'docker cp of scoped store into {name} failed: {cp.stderr.decode().strip()}'
    )
  # `docker start -a -i` reattaches the TTY/stdin and returns the exit code; --rm
  # (set at create) removes the container — and its scoped secrets — on exit.
  result = subprocess.run(['docker', 'start', '-a', '-i', container_id])
  # `--bare` (the `--bro` flow) runs claude hooks-free, so the in-container
  # session-log hooks never upload the transcript — sync it host-side now, before
  # any `drop` removes it. native sessions keep self-uploading via their hooks.
  if '--bare' in command:
    _sync_container_log(name, proj)
  if drop:
    try:
      ContainerWorkspace(name, proj).remove()
      log.info('removed container workspace %s', name)
    except RuntimeError as e:
      log.warning('could not fully remove container workspace %s: %s', name, e)
  return result.returncode
