import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from base import log
from cw.paths import _project_root

CONTAINER_DIR = Path(__file__).resolve().parent.parent / 'setup' / 'container'

_DOCKER_FORWARD_ENV = (
  'CW_BRO',
  'CW_COMMAND',
  'CW_SESSION_CONTEXT',
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

# the global ~/.claude/settings.json for container sessions: UX prefs only,
# built from scratch so host settings (permissions, hooks, model/effort) don't
# leak in. the repo's /workspace/.claude/settings.json layers on top.
_CONTAINER_SETTINGS_JSON: dict = {
  'spinnerVerbs': {'mode': 'replace', 'verbs': ['Thinking']},
  'spinnerTipsEnabled': False,
  'prefersReducedMotion': True,
  'feedbackSurveyRate': 0,
  'tui': 'fullscreen',
  # silent when healthy (Claude's default bar); a red warning pinned on-screen
  # when session-log sync is failing — the one channel Claude doesn't hide
  # behind its alternate-screen buffer (the entrypoint can't print a banner that
  # survives the session)
  'statusLine': {'type': 'command', 'command': 'session-log-statusline'},
  # enable the pyright-lsp Python language server. the plugin itself is installed
  # at image-build time and seeded into ~/.claude/plugins by the entrypoint;
  # enabling alone is not enough (claude would prompt to install it on .py files).
  'enabledPlugins': {'pyright-lsp@claude-plugins-official': True},
}


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
  is bound to that mount. takes the mount path (not name+proj) so this stays a
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


def _image_tag() -> str:
  h = hashlib.sha256()
  proj = _project_root()
  inputs = sorted(CONTAINER_DIR.iterdir()) + [proj / 'pyproject.toml', proj / 'uv.lock']
  for path in inputs:
    if path.is_file():
      h.update(path.name.encode())
      h.update(b'\0')
      h.update(path.read_bytes())
  return f'ppp-cw:{h.hexdigest()[:12]}'


def _ensure_image(tag: str) -> None:
  inspect = subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True, text=True)
  if inspect.returncode == 0:
    return
  version = (CONTAINER_DIR / 'claude-code-version').read_text().strip()
  log.info('building %s (claude-code %s)', tag, version)
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
      f'proj={_project_root()}',
      str(CONTAINER_DIR),
    ],
    check=True,
  )


def _docker_create_argv(
  tag: str,
  name: str,
  proj: Path,
  session: Path,
  command: list[str],
  *,
  docker_sock: bool = True,
  extra_env: Optional[Mapping[str, str]] = None,
  forward_bro: bool = True,
) -> list[str]:
  """argv for `docker create` of the session container (run-equivalent, unstarted).

  `docker create -it --rm --init …` then `docker start -a -i <id>` reproduces `docker
  run -it --rm --init` exactly (TTY, signals, exit code, auto-remove on exit). Splitting them
  gives `run_in_container` a window to `docker cp` the scoped credential store into
  the pre-start container's writable layer — no host-side store, no bind mount.

  `extra_env` adds explicit `-e KEY=VALUE` entries (value set here) — distinct from the
  `_DOCKER_FORWARD_ENV` loop, which forwards a host var by name.

  `forward_bro=False` drops `CW_BRO` from that forward set: the container uses it
  to theme `cw banner` and, for non-`--bare` themed sessions, to drive `cw
  populate-bro-skills` — both Claude-Code-only. an LLM-process container
  (`ask`/`do-task`/`call`) runs neither, so it must not inherit the calling
  session's ambient `CW_BRO` and pay for a pointless skills populate.
  """
  # function-local to break the docker <-> containers import cycle: containers.py
  # imports the docker helpers at module level, so docker.py keeps no top-level
  # containers import and defers this one reference to call time.
  from cw.containers import _seed_container_claude_json

  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  claude_dir.mkdir(parents=True, exist_ok=True)
  # seed-once container-private ~/.claude.json (see module docstring)
  claude_json = _seed_container_claude_json(claude_dir, home / '.claude.json')
  (claude_dir / 'settings.json').write_text(json.dumps(_CONTAINER_SETTINGS_JSON))
  argv = [
    'docker',
    'create',
    '-it',
    '--rm',
    # tini as pid 1 reaps orphaned grandchildren. our entrypoint re-execs into
    # claude, so without this pid 1 is claude — which doesn't wait() on orphans, so
    # every group-killed pipeline (spawn.run's timeout path: the dev bro's bash/grep,
    # infra deploys) would leak a zombie grandchild for the container's lifetime.
    '--init',
    '-v',
    f'{session}:/workspace',
    '-v',
    f'{proj}:/host-repo:ro',
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
  for var in _DOCKER_FORWARD_ENV:
    if var == 'CW_BRO' and not forward_bro:
      continue
    if os.environ.get(var) is not None:
      argv += ['-e', var]
  if extra_env is not None:
    for key, value in extra_env.items():
      argv += ['-e', f'{key}={value}']
  return [*argv, tag, *command]
