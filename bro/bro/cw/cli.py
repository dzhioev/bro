#!/usr/bin/env python
"""launch claude, optionally in an isolated docker container.

host mode (default): cw owns the worktree lifecycle — it creates the worktree
(`var/cw/worktrees/<name>`, `worktree-<name>` branch + submodule alternates),
provisions it with the shared setup/provision_repo.sh (same as the container
entrypoint), then runs plain `claude` from inside it (not `claude -w`, so no
claude-side worktree/provisioning hooks). On exit it drops the worktree (`--drop`)
or, interactively, offers to. cw writes its pid to the per-worktree git admin dir
so `cw list`/`clean` can tell a session is live.

container mode (--container): /workspace is a fresh clone, not a worktree — the
gitfile-based worktree layout doesn't survive the container boundary, and this
keeps the container's git state genuinely isolated. layout:

  - var/cw/containers/<name>/ on the host → /workspace rw
    (empty on first run; entrypoint clones host repo into it)
  - host project root → /host-repo ro
    (clone --shared reads objects from here via alternates; also the source for
    local submodule clones to avoid needing ssh keys in the container)
  - ~/.claude.json: not bind-mounted from host. Constructed per workspace from
    an explicit config + the host's account-identity fields into a container-
    private file at cw-sessions/<name>/.claude.json and bind-mounted from there,
    so per-project state mutations (mcpServers, allowedTools,
    hasTrustDialogAccepted) stay in the container and can't be used to escalate
    to code execution in the next host claude session.
  - ~/.claude: not seeded from host. cw-sessions/<name>/ is mounted as the
    container's ~/.claude and gets the constructed settings.json; host machine
    state stays on the host.
  - ~/.claude/.credentials.json: not bind-mounted from host. Seeded into
    cw-sessions/<name>/.credentials.json before launch (if host is fresher)
    and synced back to host on exit (if container is fresher), keyed on
    claudeAiOauth.expiresAt. Removes the runtime token-swap vector while
    preserving OAuth refresh.
  - a per-launch scoped credential store at /home/cw/.ppp: the host resolves only
    the secrets the session uses into an in-memory tar and `docker cp`s it into
    the container before it starts (no host-side store, no bind mount), with a
    credentials.json that bounds the container's registry to them. Living in the
    container's own writable layer, the store is removed with the container on
    --rm exit (or by `cw clean`), so plaintext secrets never linger on the host.
    github and aws arrive as declared secrets in this store (wired into git / the
    aws CLI by their install hooks), so there is no out-of-band github-token
    bind-mount and no ~/.aws mount.

network is not restricted by design.
"""

import concurrent.futures
import datetime
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Optional

import humanize

import session_log_health
from base import credentials, log
from base.args import REMAINDER, Parser
from base.yesno import yesno

CONTAINER_DIR = Path(__file__).resolve().parent / 'setup' / 'container'
_PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'
# auto-injected into every `cw ss` session via --append-system-prompt. Files in
# `shared/` also flow into every bro (via bro/bro.py:_load_shared_prompts), so
# put cross-surface conventions there. Top-level files here are Claude-Code-only:
# - `environment.md` is the single source of truth for the cw-banner playbook —
#   same file is reachable from bros via `FileSource` (bro/bros/ppp_dev).
# - `tool_names.md` is the Claude-Code tool-name resolution rule (`ns::tool` →
#   `mcp__ns__tool`); the bro counterpart is the framework `## Tool names` block
#   in bro/bro.py (bros resolve to `ns__tool`, no `mcp__`). Kept harness-specific
#   on purpose — do not give a bro a `FileSource` for this file.
_BASE_PROMPT_DIRS = ['shared']
_BASE_PROMPT_FILES = ['environment.md', 'tool_names.md']


def _load_base_prompts() -> str:
  parts = []
  for subdir in _BASE_PROMPT_DIRS:
    for p in sorted((_PROMPTS_DIR / subdir).glob('*')):
      if p.is_file():
        parts.append(p.read_text().strip())
  for name in _BASE_PROMPT_FILES:
    p = _PROMPTS_DIR / name
    if p.is_file():
      parts.append(p.read_text().strip())
  return '\n\n'.join(parts)


def _session_append_prompt(auto: bool, bro_name: Optional[str]) -> str:
  """--append-system-prompt text for a non --bro `cw ss` session.

  base prompts plus, when launched for a bro (dive-in sets CW_BRO), that bro's
  persona — so dive-in carries ppp-dev's policies even though it runs the native
  Claude Code harness rather than --bro.
  """
  parts = [_load_base_prompts()]
  if bro_name is not None:
    from bro.registry import create_bro

    parts.append(create_bro(bro_name).persona)
  if auto:
    parts.append('Land mode: PR')
  return '\n\n'.join(parts)


_DOCKER_FORWARD_ENV = (
  'CW_BRO',
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
_BRO_GIT_NAME = 'Bro'
_BRO_GIT_EMAIL = 'dzhioev+bro@gmail.com'

# secrets every containerized claude code session resolves regardless of bro: the
# sync-session-log hooks run in-container, and an in-session bro run records to trails.
_CW_SESSION_BASELINE = ('session_log', 'trails')

# the bro a no-`--bro` container session themes as (dive-in already sets CW_BRO to
# this); bounds a manual `cw ss -c` session's scoped credentials.
_DEFAULT_CW_BRO = 'ppp-dev'


def _load_anthropic_key() -> Optional[str]:
  """return the api_key from the `anthropic` secret, or None if missing/invalid."""
  try:
    config = credentials.get_json('anthropic')
  except credentials.SecretNotFound:
    return None
  key = config.get('api_key')
  if not isinstance(key, str) or len(key) == 0:
    return None
  return key


def _venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def _git_out(*args: str, cwd: Optional[str] = None) -> str:
  return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


def _project_root() -> Path:
  return Path(_git_out('rev-parse', '--git-common-dir')).resolve().parent


def _worktrees_dir(proj: Path) -> Path:
  return proj / 'var' / 'cw' / 'worktrees'


def _containers_dir(proj: Path) -> Path:
  return proj / 'var' / 'cw' / 'containers'


def _keychain_credentials() -> Optional[dict]:
  if platform.system() != 'Darwin':
    return None
  try:
    raw = subprocess.check_output(
      ['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
      text=True,
      stderr=subprocess.DEVNULL,
    ).strip()
    return json.loads(raw)
  except (subprocess.CalledProcessError, json.JSONDecodeError):
    return None


def _credentials_expiry(path: Path) -> int:
  if not path.is_file():
    return 0
  try:
    return json.loads(path.read_text()).get('claudeAiOauth', {}).get('expiresAt', 0)
  except (json.JSONDecodeError, OSError):
    return 0


def _sync_credentials(src: Path, dst: Path) -> None:
  """copy src → dst if src's claudeAiOauth.expiresAt is newer than dst's."""
  src_expiry = _credentials_expiry(src)
  if src_expiry == 0:
    return
  if src_expiry > _credentials_expiry(dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o600)


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
# in (oauth tokens live in .credentials.json; these hold the matching metadata).
_CLAUDE_JSON_IDENTITY_KEYS = ('oauthAccount', 'userID')

# the global ~/.claude/settings.json for container sessions: UX prefs only,
# built from scratch so host settings (permissions, hooks, model/effort) don't
# leak in. the repo's /workspace/.claude/settings.json layers on top.
_CONTAINER_SETTINGS_JSON: dict = {
  'spinnerVerbs': {'mode': 'replace', 'verbs': ['Thinking']},
  'spinnerTipsEnabled': False,
  'prefersReducedMotion': True,
  'feedbackSurveyRate': 0,
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

  `forward_bro=False` drops `CW_BRO` from that forward set: the only thing the
  container does with it is `cw populate-bro-skills` (Claude Code slash-command
  discovery), so an LLM-process container (`ask`/`do-task`/`call`) that never runs
  Claude Code must not inherit the calling session's ambient `CW_BRO` and pay for a
  pointless skills populate.
  """
  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  claude_dir.mkdir(parents=True, exist_ok=True)
  # seed-once container-private ~/.claude.json (see module docstring)
  claude_json = _seed_container_claude_json(claude_dir, home / '.claude.json')
  (claude_dir / 'settings.json').write_text(json.dumps(_CONTAINER_SETTINGS_JSON))
  # credentials: on macOS the keychain may be fresher than the file (e.g. after a
  # host-mode login that updated the keychain but not the file) — pick the more
  # recent source for the host file, then sync host → container-private so the
  # container starts with the freshest tokens. post-run, we'll sync back if the
  # container refreshed during the session.
  host_creds = home / '.claude' / '.credentials.json'
  keychain_creds = _keychain_credentials()
  if keychain_creds is not None:
    keychain_expiry = keychain_creds.get('claudeAiOauth', {}).get('expiresAt', 0)
    if keychain_expiry > _credentials_expiry(host_creds):
      host_creds.write_text(json.dumps(keychain_creds))
      host_creds.chmod(0o600)
  _sync_credentials(host_creds, claude_dir / '.credentials.json')
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


def _host_pidfile(proj: Path, name: str) -> Path:
  # per-worktree git admin dir (outside the working tree, so it never shows up in
  # `git status` and is cleaned up with the worktree). `cw` writes its own pid here
  # for the session's duration.
  return proj / '.git' / 'worktrees' / name / 'cw-session.pid'


def _is_local_active(name: str) -> bool:
  # host sessions run plain `claude` (no `-w`), so cw is the worktree's owner for
  # the session; its pid in the lockfile, still alive, means the session is active.
  pidfile = _host_pidfile(_project_root(), name)
  if not pidfile.is_file():
    return False
  try:
    pid = int(pidfile.read_text().strip())
  except ValueError:
    return False
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  except PermissionError:
    return True
  return True


def _running_container_mounts() -> set[str]:
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


def _latest_jsonl(projects_dir: Path) -> Optional[Path]:
  if not projects_dir.is_dir():
    return None
  jsonls = [p for p in projects_dir.iterdir() if p.suffix == '.jsonl']
  if len(jsonls) == 0:
    return None
  return max(jsonls, key=lambda p: p.stat().st_mtime)


def _projects_dir_for_local(name: str, proj: Path) -> Path:
  worktree = _worktrees_dir(proj) / name
  encoded = str(worktree).replace('/', '-').replace('.', '-')
  return Path.home() / '.claude' / 'projects' / encoded


def _projects_dir_for_container(name: str) -> Path:
  return Path.home() / '.claude' / 'cw-sessions' / name / 'projects' / '-workspace'


def _read_subject(projects_dir: Path) -> Optional[str]:
  latest = _latest_jsonl(projects_dir)
  if latest is None:
    return None
  try:
    f = latest.open()
  except OSError:
    return None
  with f:
    for line in f:
      try:
        d = json.loads(line)
      except json.JSONDecodeError:
        continue
      if d.get('type') != 'user' or d.get('isSidechain') is True:
        continue
      content = d.get('message', {}).get('content')
      text: Optional[str] = None
      if isinstance(content, str):
        text = content
      elif isinstance(content, list):
        for c in content:
          if isinstance(c, dict) and c.get('type') == 'text':
            text = c.get('text')
            break
      if text is None:
        continue
      stripped = text.lstrip()
      if stripped.startswith('<'):
        continue
      first_line = stripped.split('\n', 1)[0].strip()
      if len(first_line) > 0:
        return first_line
  return None


def _last_active(worktree: Path) -> Optional[float]:
  if not worktree.is_dir():
    return None
  result = subprocess.run(
    ['find', str(worktree), '-not', '-path', '*/.git/*', '-type', 'f', '-printf', '%T@\n'],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0 or len(result.stdout.strip()) == 0:
    return None
  return max(float(line) for line in result.stdout.splitlines() if len(line) > 0)


def _format_age(mtime: float) -> str:
  delta = datetime.timedelta(seconds=int(datetime.datetime.now().timestamp() - mtime))
  return humanize.naturaltime(delta)


def _truncate(s: str, n: int) -> str:
  return s if len(s) <= n else s[: n - 1] + '…'


_BADGES = {'L': '[.]', 'C': '[o]', 'X': '[x]'}
_KIND_ORDER = {'L': 0, 'C': 1, 'X': 2}
_CONTAINER_PREFIX = 'c:'

# six-line block-letter "B R O" rendered with box-drawing characters;
# shown on top of the `cw banner` output in --bro sessions.
_BRO_LOGO = """\
██████╗   ██████╗    ██████╗
██╔══██╗  ██╔══██╗  ██╔═══██╗
██████╔╝  ██████╔╝  ██║   ██║
██╔══██╗  ██╔══██╗  ██║   ██║
██████╔╝  ██║  ██║  ╚██████╔╝
╚═════╝   ╚═╝  ╚═╝   ╚═════╝\
"""


def _in_container() -> bool:
  """detect a container by /.dockerenv presence. extracted so tests can stub it."""
  return Path('/.dockerenv').is_file()


# tokens after which the rest of an unquoted launch command is the user-typed
# prompt — `dive-in --new <seed>`, `cw ss -p <prompt>`, `cw ss ... -- <prompt>`.
# rfind so the *last* marker wins if more than one is present.
_PROMPT_MARKERS = (' --new ', ' --prompt ', ' -p ', ' -- ')


def _split_launch_prompt(command: str) -> tuple[str, Optional[str]]:
  """split a launch command into (prefix, prompt) at the prompt marker, if any.

  prefix keeps the marker token (e.g. 'dive-in --new ') so callers can append a
  placeholder. returns (command, None) when no marker is present or nothing
  follows it.
  """
  for marker in _PROMPT_MARKERS:
    idx = command.rfind(marker)
    if idx < 0:
      continue
    head = command[: idx + len(marker)]
    tail = command[idx + len(marker) :].strip()
    if len(tail) > 0:
      return head, tail
  return command, None


def _session_facts() -> dict[str, Optional[str | bool]]:
  """collect session facts from env + /.dockerenv for `cw banner`.

  read-only; never raises. callers decide whether to render visually or for an
  LLM tool result. Fields:
    - in_container (bool) — /.dockerenv presence
    - name (Optional[str]) — workspace name (CW_NAME)
    - bro (Optional[str]) — bro persona (CW_BRO), only set under `cw ss --bro`
    - host_workspace (Optional[str]) — host-side path to the workspace dir
    - container_workspace (Optional[str]) — '/workspace' inside a container, else None
    - exec_command (Optional[str]) — `cw exec <name>` for container sessions
    - cw_command (Optional[str]) — the canonical `cw ss …` invocation (CW_COMMAND)
    - shell_command (Optional[str]) — the outer launch command (PPP_SHELL_COMMAND).
      For wrappers like dive-in, this differs from cw_command; for direct `cw ss`
      use, the two are equal and the banner suppresses the duplicate
    - prompt (Optional[str]) — the user-typed prompt extracted from shell_command
      when a `--new`/`-p`/`--prompt`/`--` marker is found; shell_command is
      shown with the prompt portion replaced by a placeholder in this case
    - sync_warning (Optional[str]) — set when the session-log sync health file
      reports a failure, so the banner can warn that logs aren't reaching S3
  """
  in_container = _in_container()
  name = os.environ.get('CW_NAME') or None
  bro = os.environ.get('CW_BRO') or None
  cw_command = os.environ.get('CW_COMMAND') or None
  shell_command = os.environ.get('PPP_SHELL_COMMAND') or cw_command
  host_workspace: Optional[str] = os.environ.get('CW_HOST_WORKSPACE') or None
  container_workspace: Optional[str] = '/workspace' if in_container else None

  if not in_container and host_workspace is None and name is not None:
    # host worktree case — derive path from the project root + worktree name
    try:
      proj = _project_root()
    except subprocess.CalledProcessError:
      proj = None
    if proj is not None:
      candidate = _worktrees_dir(proj) / name
      if candidate.is_dir():
        host_workspace = str(candidate)

  exec_command = f'cw exec {name}' if in_container and name is not None else None

  prompt: Optional[str] = None
  if shell_command is not None:
    shell_command, prompt = _split_launch_prompt(shell_command)

  sync_warning: Optional[str] = None
  if session_log_health.is_failing():
    sync_warning = 'session-log sync FAILING — run setup/bootstrap_session_log.sh'

  return {
    'in_container': in_container,
    'name': name,
    'bro': bro,
    'host_workspace': host_workspace,
    'container_workspace': container_workspace,
    'exec_command': exec_command,
    'cw_command': cw_command,
    'shell_command': shell_command,
    'prompt': prompt,
    'sync_warning': sync_warning,
  }


def _render_banner_visual(facts: dict[str, Optional[str | bool]]) -> str:
  """render the banner with ANSI colour + the Bro logo for bro sessions."""
  red = '\033[31m'
  bold = '\033[1m'
  bold_white = '\033[1;97m'  # bright-white bold — emphasis for the @prompt@ slot
  dim = '\033[2m'
  reset = '\033[0m'

  lines: list[str] = []
  if facts['sync_warning'] is not None:
    # most prominent slot — above the logo, red+bold so a broken sync is the
    # first thing the eye lands on in a `cw exec` shell
    lines.append(f'{red}{bold}⚠ {facts["sync_warning"]}{reset}')
    lines.append('')
  if facts['bro'] is not None:
    # annotate the bottom line of the logo with a `// <bro>` signature — dim
    # slashes (comment style), bro name in bright-white bold so it stands out
    logo_lines: list[str] = list(_BRO_LOGO.split('\n'))
    logo_lines[-1] = f'{logo_lines[-1]} {dim}//{reset} {bold_white}{facts["bro"]}{reset}'
    lines.extend(logo_lines)
    lines.append('')

  # collect rows as (label, label_style, value) — label_style is applied to
  # the padded label so width math runs on the raw text, not on ANSI bytes
  raw_name = facts['name'] if isinstance(facts['name'], str) else '(unnamed)'
  display_name = _format_ref(raw_name, bool(facts['in_container']))
  rows: list[tuple[str, str, str]] = [
    ('cw session:', '', f'{bold}{display_name}{reset}'),
  ]

  # `cw command` is the canonical `cw ss …` invocation; suppress when it's
  # the same string as `launched` (direct `cw ss` use) so we don't show the
  # same text twice
  if facts['cw_command'] is not None and facts['cw_command'] != facts['shell_command']:
    rows.append(('cw command:', '', f'{dim}{facts["cw_command"]}{reset}'))

  if facts['in_container']:
    # /workspace inside, host bind-mount path below — both are useful and
    # packing them onto one line crowded the eye
    rows.append(('workspace:', '', str(facts['container_workspace'])))
    hp = facts['host_workspace']
    if hp is not None:
      rows.append(('host path:', '', f'{dim}{hp}{reset}'))
  else:
    hp = facts['host_workspace']
    # host-mode worktree path printed in red as a "this is your actual repo
    # on disk — careless edits leak out of the session" reminder
    if hp is not None:
      rows.append(('workspace:', '', f'{red}{hp}{reset}'))
    else:
      rows.append(
        ('workspace:', '', f'{dim}(unknown — no CW_NAME / not a registered worktree){reset}')
      )

  if facts['exec_command'] is not None:
    # "docker shell" because the command opens a shell *inside* the docker
    # container — the label tracks the destination, not the host that launches it
    rows.append(('docker shell:', '', f'{dim}{facts["exec_command"]}{reset}'))

  if facts['shell_command'] is not None:
    launched = f'{dim}{facts["shell_command"]}{reset}'
    if facts['prompt'] is not None:
      launched += f'{bold_white}@prompt@{reset}'
    rows.append(('launched:', '', launched))

  if facts['prompt'] is not None:
    rows.append(('prompt:', bold_white, str(facts['prompt'])))

  # auto-align the value column to one space past the widest label
  width = max(len(label) for label, _, _ in rows)
  for label, label_style, value in rows:
    padded = label.ljust(width)
    styled_label = f'{label_style}{padded}{reset}' if len(label_style) > 0 else padded
    lines.append(f'{styled_label} {value}')

  return '\n'.join(lines)


def _render_banner_llm(facts: dict[str, Optional[str | bool]]) -> str:
  """render the banner as plain key:value lines for an LLM Bash tool result.

  cw_command is suppressed when it equals launch_command (no wrapper involved).
  The user prompt is deliberately *not* emitted — the LLM already has it as
  the first message of the conversation, so re-printing it would just burn
  context. launch_command keeps its trailing marker (e.g. `dive-in --new `)
  as the signal that a seed prompt exists.
  """
  lines: list[str] = []
  if facts['sync_warning'] is not None:
    # first line so it lands in Claude's collapsed tool-output preview without
    # needing expansion; the agent should relay it to the user
    lines.append('session_log_sync: FAILING — run setup/bootstrap_session_log.sh')
  lines.append(f'kind: {"container" if facts["in_container"] else "host worktree"}')
  pairs: list[tuple[str, str]] = [
    ('name', 'name'),
    ('bro', 'bro'),
    ('host_workspace', 'workspace_host_path'),
    ('container_workspace', 'workspace_container_path'),
    ('exec_command', 'docker_shell_command'),
  ]
  if facts['cw_command'] is not None and facts['cw_command'] != facts['shell_command']:
    pairs.append(('cw_command', 'cw_command'))
  pairs.append(('shell_command', 'launch_command'))
  for key, label in pairs:
    value = facts[key]
    if value is not None:
      lines.append(f'{label}: {value}')
  return '\n'.join(lines)


def banner(llm: bool) -> int:
  """print the banner. visual by default; --llm for plain text."""
  facts = _session_facts()
  rendered = _render_banner_llm(facts) if llm else _render_banner_visual(facts)
  print(rendered)
  return 0


def _format_ref(name: str, is_container: bool) -> str:
  return f'{_CONTAINER_PREFIX}{name}' if is_container else name


def _parse_ref(ref: str) -> tuple[str, bool]:
  if ref.startswith(_CONTAINER_PREFIX):
    return ref[len(_CONTAINER_PREFIX) :], True
  return ref, False


def _find_container_id(name: str, proj: Path) -> Optional[str]:
  """find the running container backing the named container workspace.

  filters `docker ps` by the workspace's host mount path, which is unique per
  workspace. returns the container short id, or None if no running container
  is bound to that mount.
  """
  session = _containers_dir(proj) / name
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


def exec_in_workspace(name: str, cmd: list[str]) -> int:
  """exec a command in the running container backing the named workspace.

  with no command, starts an interactive bash. either way, `/workspace/.venv`
  is sourced first so the workspace's console scripts (created by `uv sync`)
  are on PATH; the prompt's `(.venv)` prefix is dropped after `.bashrc` re-runs,
  but VIRTUAL_ENV and PATH survive.
  """
  if name.startswith(_CONTAINER_PREFIX):
    name = name[len(_CONTAINER_PREFIX) :]
  proj = _project_root()
  container_id = _find_container_id(name, proj)
  if container_id is None:
    log.error('no running container for workspace %r', name)
    return 1
  if len(cmd) == 0:
    docker_cmd = ['bash', '-c', 'source /workspace/.venv/bin/activate 2>/dev/null; exec bash']
  else:
    docker_cmd = [
      'bash',
      '-c',
      'source /workspace/.venv/bin/activate 2>/dev/null; exec "$@"',
      'cw-exec',
      *cmd,
    ]
  # run as cw, not the image's default root: docker exec ignores the entrypoint's
  # gosu drop, so without -u every exec'd command runs as root and writes
  # root-owned files into the bind-mounted /workspace that the host user can't
  # later remove. the entrypoint remaps cw to the host uid, so -u cw matches the
  # session user and keeps workspace files host-owned.
  return subprocess.run(['docker', 'exec', '-it', '-u', 'cw', container_id, *docker_cmd]).returncode


def _resolve_workspace(ref: str, proj: Path) -> tuple[Path, Optional[Path]]:
  name, is_container = _parse_ref(ref)
  if is_container:
    path = _containers_dir(proj) / name
    if not path.is_dir():
      raise ValueError(f'container workspace not found: {ref}')
    return path, proj
  path = _worktrees_dir(proj) / name
  if not path.is_dir():
    raise ValueError(f'workspace not found: {ref}')
  return path, None


def _list_entry_local(p: Path, proj: Path) -> tuple[str, bool, str, Optional[str], Optional[float]]:
  kind = 'L' if _is_local_active(p.name) else 'X'
  pdir = _projects_dir_for_local(p.name, proj)
  return (kind, False, p.name, _read_subject(pdir), _last_active(p))


def _list_entry_container(
  p: Path, mounts: set[str]
) -> tuple[str, bool, str, Optional[str], Optional[float]]:
  kind = 'C' if str(p) in mounts else 'X'
  pdir = _projects_dir_for_container(p.name)
  return (kind, True, p.name, _read_subject(pdir), _last_active(p))


def list_workspaces() -> int:
  proj = _project_root()
  worktrees_dir = _worktrees_dir(proj)
  containers_dir = _containers_dir(proj)

  local_dirs = [p for p in worktrees_dir.iterdir() if p.is_dir()] if worktrees_dir.is_dir() else []
  container_dirs = (
    [p for p in containers_dir.iterdir() if p.is_dir()] if containers_dir.is_dir() else []
  )

  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(_running_container_mounts) if len(container_dirs) > 0 else None
    local_futures = [pool.submit(_list_entry_local, p, proj) for p in local_dirs]
    mounts = mounts_future.result() if mounts_future is not None else set()
    container_futures = [pool.submit(_list_entry_container, p, mounts) for p in container_dirs]
    entries = [f.result() for f in local_futures] + [f.result() for f in container_futures]

  if len(entries) == 0:
    return 0
  entries.sort(key=lambda e: (_KIND_ORDER[e[0]], e[1], e[2]))
  displays = [_format_ref(name, is_container) for _, is_container, name, _, _ in entries]
  name_w = max(len(d) for d in displays)
  ages = [_format_age(mtime) if mtime is not None else '' for _, _, _, _, mtime in entries]
  age_w = max(len(a) for a in ages) if len(ages) > 0 else 0
  for (kind, _, _, subject, _), display, age in zip(entries, displays, ages):
    badge = _BADGES[kind]
    age_col = f'  {age:<{age_w}}' if len(age) > 0 else ' ' * (age_w + 2)
    if subject is None:
      print(f'{badge} {display:<{name_w}}{age_col}')
    else:
      print(f'{badge} {display:<{name_w}}{age_col}  {_truncate(subject, 80)}')
  return 0


def _worktree_is_clean(
  path: Path, container_proj: Optional[Path] = None, refresh_origin: bool = True
) -> tuple[bool, list[str]]:
  """check whether a worktree is safe to remove.

  returns (safe, reasons) where reasons lists what prevents removal.
  container_proj: when set, `path` is a container clone whose own remotes
  are unreachable from the host (origin = HTTPS GitHub without creds, host
  remote = /host-repo bind mount). Ancestry checks run in container_proj
  (resp. container_proj/<sub_path>) instead; container_proj/.git/objects is
  exposed as an alternate so git ops in the container clone can resolve their
  /host-repo alternates, and the clone's own object store is exposed as an
  alternate to container_proj so the ancestry walk can reach the container's
  local commits without writing them into the shared repo.
  refresh_origin: fetch origin/master before the ancestry check. callers that
  run many checks concurrently (clean_workspaces) fetch once up front and pass
  False — a per-check fetch into the shared repo races on the ref lock, and the
  old FETCH_HEAD-based bring-in raced on the single shared FETCH_HEAD file.
  """
  no_prompt_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
  local_env = dict(no_prompt_env)
  if container_proj is not None:
    if not (path / '.git').exists():
      return False, ['not a git repository']
    local_env['GIT_ALTERNATE_OBJECT_DIRECTORIES'] = str(container_proj / '.git' / 'objects')
  reasons: list[str] = []
  status = subprocess.run(
    ['git', 'status', '--porcelain'], cwd=path, capture_output=True, text=True, env=local_env
  )
  if status.returncode != 0:
    reasons.append('cannot read git status')
    return False, reasons
  if len(status.stdout.strip()) > 0:
    reasons.append('uncommitted or untracked changes')

  check_root = container_proj if container_proj is not None else path
  origin_ok = True
  if refresh_origin:
    fetch = subprocess.run(
      ['git', 'fetch', '--quiet', 'origin', 'master'],
      cwd=check_root,
      capture_output=True,
      env=no_prompt_env,
    )
    origin_ok = fetch.returncode == 0
    if not origin_ok:
      reasons.append('could not fetch origin/master')
  if origin_ok:
    # resolve the ref to compare against origin/master, plus the object store(s)
    # the ancestry walk needs. for a container clone, read its HEAD sha and
    # expose the clone's objects to check_root as a read-only alternate — the
    # walk then reaches the container's local commits without fetching them into
    # the shared repo (which would race on FETCH_HEAD across concurrent checks).
    ancestry_env = dict(no_prompt_env)
    if container_proj is not None:
      head = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=path, capture_output=True, text=True, env=local_env
      )
      head_ref = head.stdout.strip() if head.returncode == 0 else None
      if head_ref is None:
        reasons.append("could not read container's HEAD")
      else:
        ancestry_env['GIT_ALTERNATE_OBJECT_DIRECTORIES'] = str(path / '.git' / 'objects')
    else:
      head_ref = 'HEAD'
    if head_ref is not None:
      master_check = subprocess.run(
        ['git', 'rev-parse', '--verify', 'origin/master'],
        cwd=check_root,
        capture_output=True,
        env=no_prompt_env,
      )
      if master_check.returncode != 0:
        reasons.append('origin/master not found')
      else:
        ancestor = subprocess.run(
          ['git', 'merge-base', '--is-ancestor', head_ref, 'origin/master'],
          cwd=check_root,
          capture_output=True,
          env=ancestry_env,
        )
        if ancestor.returncode != 0:
          ahead = subprocess.run(
            ['git', 'rev-list', '--count', head_ref, '^origin/master'],
            cwd=check_root,
            capture_output=True,
            text=True,
            env=ancestry_env,
          )
          n = ahead.stdout.strip() if ahead.returncode == 0 else '?'
          reasons.append(f'{n} commit(s) not on origin/master')

  sub_status = subprocess.run(
    ['git', 'submodule', 'status'], cwd=path, capture_output=True, text=True, env=local_env
  )
  if sub_status.returncode == 0:
    for line in sub_status.stdout.strip().splitlines():
      stripped = line.strip()
      if stripped.startswith('-'):
        continue
      parts = stripped.lstrip('+').split()
      if len(parts) < 2:
        continue
      sub_hash, sub_path = parts[0], parts[1]
      sub_check_root = container_proj / sub_path if container_proj is not None else path / sub_path
      sub_fetch = subprocess.run(
        ['git', 'fetch', '--quiet', 'origin'],
        cwd=sub_check_root,
        capture_output=True,
        env=no_prompt_env,
      )
      if sub_fetch.returncode != 0:
        reasons.append(f'submodule {sub_path}: could not fetch origin')
        continue
      if container_proj is not None:
        sub_bring_in = subprocess.run(
          ['git', 'fetch', '--quiet', str(path / sub_path), 'HEAD'],
          cwd=sub_check_root,
          capture_output=True,
          env=no_prompt_env,
        )
        if sub_bring_in.returncode != 0:
          reasons.append(f"submodule {sub_path}: could not fetch container's HEAD")
          continue
      sub_default = subprocess.run(
        ['git', 'rev-parse', '--verify', 'origin/HEAD'],
        cwd=sub_check_root,
        capture_output=True,
        env=no_prompt_env,
      )
      remote_ref = 'origin/HEAD' if sub_default.returncode == 0 else 'origin/master'
      sub_ancestor = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', sub_hash, remote_ref],
        cwd=sub_check_root,
        capture_output=True,
        env=no_prompt_env,
      )
      if sub_ancestor.returncode != 0:
        reasons.append(f'submodule {sub_path}: commit {sub_hash[:8]} not pushed to remote')

  return len(reasons) == 0, reasons


def _cleanup_image() -> Optional[str]:
  """a locally-present ppp-cw image usable to delete root-owned container files.

  prefers the current image tag, then any other locally-present ppp-cw image.
  returns None when none exist (nothing to escalate the removal with).
  """
  tag = _image_tag()
  if subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True).returncode == 0:
    return tag
  listed = subprocess.run(
    ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', 'ppp-cw'],
    capture_output=True,
    text=True,
  )
  for line in listed.stdout.splitlines():
    candidate = line.strip()
    if len(candidate) > 0 and '<none>' not in candidate:
      return candidate
  return None


def _remove_container_dir(path: Path, image: Optional[str]) -> None:
  """remove a container workspace dir, including files the host user can't unlink.

  container processes can leave files owned by uids that don't match the host
  user (e.g. a pre-fix `cw exec` ran as root, or root-running tooling reached
  the docker socket), which a host-side rmtree hits EPERM on. try a plain rmtree
  first, then escalate to deleting from inside a throwaway root container, which
  can unlink regardless of owner. raises RuntimeError if removal fails.
  """
  try:
    shutil.rmtree(path)
    return
  except FileNotFoundError:
    return
  except PermissionError:
    pass
  if image is None:
    raise RuntimeError(
      f'{path}: contains files owned by an in-container uid and no ppp-cw image '
      'is available to remove them as root'
    )
  result = subprocess.run(
    # override the entrypoint and force uid 0 so `rm` runs as root inside the
    # container; mount the host-owned parent so it can delete the child tree
    [
      'docker',
      'run',
      '--rm',
      '-u',
      '0',
      '--entrypoint',
      'rm',
      '-v',
      f'{path.parent}:/target',
      image,
      '-rf',
      f'/target/{path.name}',
    ],
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    raise RuntimeError(f'{path}: docker rm failed: {result.stderr.strip()}')
  if path.exists():
    raise RuntimeError(f'{path}: still present after docker rm')


def clean_workspaces(
  force: bool = False, dry_run: bool = False, refs: Optional[list[str]] = None
) -> int:
  proj = _project_root()
  worktrees_dir = _worktrees_dir(proj)
  containers_dir = _containers_dir(proj)

  filter_refs = set(refs) if refs is not None and len(refs) > 0 else None
  if filter_refs is not None:
    available: set[str] = set()
    if worktrees_dir.is_dir():
      available.update(p.name for p in worktrees_dir.iterdir() if p.is_dir())
    if containers_dir.is_dir():
      available.update(_format_ref(p.name, True) for p in containers_dir.iterdir() if p.is_dir())
    missing = filter_refs - available
    if len(missing) > 0:
      log.error('workspace(s) not found: %s', ', '.join(sorted(missing)))
      return 1

  local_dirs = sorted(
    p
    for p in (worktrees_dir.iterdir() if worktrees_dir.is_dir() else [])
    if p.is_dir() and (filter_refs is None or p.name in filter_refs)
  )
  container_dirs = sorted(
    p
    for p in (containers_dir.iterdir() if containers_dir.is_dir() else [])
    if p.is_dir() and (filter_refs is None or _format_ref(p.name, True) in filter_refs)
  )

  # fetch origin/master once up front: the per-workspace checks below run
  # concurrently and share proj's (resp. the common dir's) refs, so a fetch
  # inside each would race on the ref lock. a stale ref only ever makes the
  # ancestry check stricter (errs toward keeping a workspace), so a failed
  # fetch is a warning, not fatal. with this done, the top-level ancestry check
  # passes refresh_origin=False and touches only read-only shared state.
  fetched = subprocess.run(
    ['git', 'fetch', '--quiet', 'origin', 'master'],
    cwd=proj,
    capture_output=True,
    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
  )
  if fetched.returncode != 0:
    log.warning('could not fetch origin/master; ancestry checks use the local ref')

  def _check_local(p: Path) -> tuple[Path, bool, bool, list[str]]:
    if _is_local_active(p.name):
      return p, True, False, []
    safe, reasons = _worktree_is_clean(p, refresh_origin=False)
    return p, False, safe, reasons

  def _check_container(p: Path, mounts: set[str]) -> tuple[Path, bool, bool, list[str]]:
    if str(p) in mounts:
      return p, True, False, []
    safe, reasons = _worktree_is_clean(p, container_proj=proj, refresh_origin=False)
    return p, False, safe, reasons

  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(_running_container_mounts) if len(container_dirs) > 0 else None
    local_results = list(pool.map(_check_local, local_dirs))
    mounts = mounts_future.result() if mounts_future is not None else set()
    container_results = list(pool.map(lambda p: _check_container(p, mounts), container_dirs))

  removed = 0
  skipped = 0
  failed = 0
  cleanup_image = _cleanup_image() if len(container_results) > 0 else None

  for p, active, safe, reasons in local_results:
    if active:
      log.info('skip %s: active session', p.name)
      skipped += 1
      continue
    if not safe:
      if not force:
        log.info('skip %s: %s', p.name, '; '.join(reasons))
        skipped += 1
        continue
      log.info('force %s: %s', p.name, '; '.join(reasons))
    if dry_run:
      log.info('would remove %s', p.name)
    else:
      branch = f'worktree-{p.name}'
      subprocess.run(
        ['git', 'worktree', 'remove', '--force', str(p)], check=False, capture_output=True
      )
      subprocess.run(['git', 'branch', '-D', branch], check=False, capture_output=True)
      log.info('removed %s', p.name)
    removed += 1

  for p, active, safe, reasons in container_results:
    ref = _format_ref(p.name, True)
    if active:
      log.info('skip %s: active session', ref)
      skipped += 1
      continue
    if not safe:
      if not force:
        log.info('skip %s: %s', ref, '; '.join(reasons))
        skipped += 1
        continue
      log.info('force %s: %s', ref, '; '.join(reasons))
    if dry_run:
      log.info('would remove %s', ref)
    else:
      try:
        _remove_container_dir(p, cleanup_image)
      except RuntimeError as e:
        log.error('skip %s: %s', ref, e)
        failed += 1
        continue
      session_dir = Path.home() / '.claude' / 'cw-sessions' / p.name
      if session_dir.is_dir():
        shutil.rmtree(session_dir, ignore_errors=True)
      log.info('removed %s', ref)
    removed += 1

  log.info('cleaned %d workspace(s), skipped %d, failed %d', removed, skipped, failed)
  return 1 if failed > 0 else 0


def _mcp_config_argv(mcp: str) -> list[str]:
  if mcp == 'local':
    return ['--mcp-config=flow/mcp/mcp_local.json']
  assert mcp == 'http'
  try:
    cfg = credentials.get_json('flow_mcp')
  except credentials.SecretNotFound:
    raise SystemExit('missing flow_mcp secret — run flow/mcp/server/bootstrap_secrets.sh')
  mcp_json = json.dumps(
    {
      'mcpServers': {
        'flow': {
          'type': 'http',
          'url': cfg['url'],
          'headers': {'Authorization': f'Bearer {cfg["token"]}'},
        },
      },
    },
    separators=(',', ':'),
  )
  return ['--mcp-config', mcp_json]


EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')


def add_forwarded_flags(parser: Parser) -> None:
  """register the flags that wrappers (dive-in, start-session) forward to `cw ss`.

  Adding a new pass-through flag here makes it available in every wrapper that
  calls this helper — no per-flag plumbing in each wrapper.
  """
  parser.add_argument(
    '--auto',
    action='store_true',
    help='let claude run autonomously, skipping most permissions (allowed only with -c)',
  )
  parser.add_argument(
    '--fast',
    action='store_true',
    help='enable fast mode for the session (disabled by default regardless of host settings)',
  )
  parser.add_argument(
    '--grant',
    action='append',
    default=None,
    metavar='SECRET',
    help='grant a secret to the container scoped set on top of the computed set (repeatable); requires -c; errors if already in the set or unknown to the registry',
  )
  parser.add_argument(
    '--revoke',
    action='append',
    default=None,
    metavar='SECRET',
    help='revoke a secret from the container scoped set (repeatable); requires -c; errors if not in the set',
  )
  parser.add_argument(
    '--effort',
    default=None,
    choices=EFFORT_LEVELS,
    help='thinking effort level (forwarded to claude --effort)',
  )
  parser.add_argument(
    '--rc',
    action='store_true',
    help='enable claude remote control (--remote-control); breaks local Ctrl+V image paste, so off by default — implied by --auto',
  )
  parser.add_argument(
    '--resume',
    action='store_true',
    help='resume the latest claude session in the named workspace; skips the initial prompt',
  )
  parser.add_argument(
    '--into',
    default=None,
    metavar='REF',
    help="base a new session on git REF (branch/tag/sha) instead of the default (the host repo's current HEAD, in both container and host mode). ignored once the workspace exists",
  )


def extract_forwarded_argv(args: dict) -> list[str]:
  """pop forwarded-flag values from `args` and return them as canonical argv tokens.

  mutates `args`: removes every key registered by `add_forwarded_flags`. The returned
  list is suitable to splice directly into a `cw ss` invocation.
  """
  parser = Parser(add_help=False)
  add_forwarded_flags(parser)
  forwarded = {
    a.dest: args.pop(a.dest)
    for a in parser._actions
    if len(a.option_strings) > 0 and a.dest in args
  }
  return parser.reconstruct(forwarded, prog=[])


def start_session(
  name: str,
  container: bool,
  drop: bool,
  auto: bool,
  fast: bool,
  grant: Optional[list[str]],
  revoke: Optional[list[str]],
  effort: Optional[str],
  rc: bool,
  resume: bool,
  into: Optional[str],
  mcp: Optional[str],
  bro: Optional[str],
  prompt: Optional[str],
  claude_args: list[str],
) -> int:
  rc = rc or auto
  grant = grant if grant is not None else []
  revoke = revoke if revoke is not None else []
  flags = {
    '-c': container,
    '--auto': auto,
    '--drop': drop,
    '--rc': rc,
    '--resume': resume,
  }
  parts = ['cw', 'ss', *(f for f, v in flags.items() if v)]
  if effort is not None:
    parts.extend(['--effort', effort])
  if mcp is not None:
    parts.append('--mcp')
    if mcp != 'http':
      parts.append(mcp)
  if bro is not None:
    parts.extend(['--bro', bro])
  for g in grant:
    parts.extend(['--grant', g])
  for r in revoke:
    parts.extend(['--revoke', r])
  if into is not None:
    parts.extend(['--into', into])
  parts.extend([name, *claude_args])
  os.environ['CW_COMMAND'] = ' '.join(parts)
  os.environ['CW_NAME'] = name
  os.environ.setdefault('PPP_SHELL_COMMAND', os.environ['CW_COMMAND'])

  if resume:
    proj = _project_root()
    projects_dir = (
      _projects_dir_for_container(name) if container else _projects_dir_for_local(name, proj)
    )
    latest = _latest_jsonl(projects_dir)
    if latest is None:
      log.error('no claude session found for %s in %s', name, projects_dir)
      return 1
    session_id = latest.stem
    log.info('resuming session %s', session_id)
    claude_args = ['--resume', session_id, *claude_args]

  # resolve --into against the host repo now (a branch/tag/sha → a sha). the
  # container reaches it via /host-repo's shared objects; the host worktree bases
  # its new branch on it. only meaningful at creation — resume reuses the existing
  # workspace, so the two are mutually exclusive (checked in main).
  base_ref: Optional[str] = None
  if into is not None:
    rev = subprocess.run(
      ['git', 'rev-parse', '--verify', f'{into}^{{commit}}'],
      cwd=_project_root(),
      capture_output=True,
      text=True,
    )
    if rev.returncode != 0:
      log.error('cannot resolve --into ref: %s', into)
      return 1
    base_ref = rev.stdout.strip()

  if auto:
    os.environ['GIT_AUTHOR_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_AUTHOR_EMAIL'] = _BRO_GIT_EMAIL
    os.environ['GIT_COMMITTER_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_COMMITTER_EMAIL'] = _BRO_GIT_EMAIL

  if bro is not None:
    # the container entrypoint reads CW_BRO and runs `cw populate-bro-skills`
    # after venv activation, so claude code's slash-command discovery picks up
    # the bro's skills from .claude/skills/<name>/SKILL.md symlinks. host-mode
    # `--bro` is unsupported (the --bare flow needs the container entrypoint to
    # wire MCP and the api-key helper).
    os.environ['CW_BRO'] = bro
    claude_args = [*_bro_claude_argv(bro), *claude_args]
    if prompt is not None:
      claude_args = [*claude_args, '--', prompt]
    secrets, docker_sock = _container_secrets(bro, mcp=mcp, bro_mode=True)
    try:
      secrets = _finalize_secrets(secrets, grant=grant, revoke=revoke)
    except ValueError as e:
      log.error('%s', e)
      return 1
    return cw(
      name=name,
      container=container,
      drop=drop,
      auto=auto,
      base_ref=base_ref,
      claude_args=claude_args,
      secrets=secrets,
      docker_sock=docker_sock,
    )

  fast_mode_settings = json.dumps({'fastMode': fast})
  inject = [
    '--disallowed-tools',
    'mcp__claude_ai_*',
    '--settings',
    fast_mode_settings,
  ]
  if rc:
    inject[:0] = ['--remote-control', name]
  if effort is not None:
    inject.extend(['--effort', effort])
  if mcp is not None:
    inject.extend(_mcp_config_argv(mcp))
  if auto:
    inject.append('--dangerously-skip-permissions')
  claude_args = [*inject, *claude_args]

  bro_env = os.environ.get('CW_BRO')
  append_prompt = _session_append_prompt(auto, bro_env)
  claude_args = [*claude_args, '--append-system-prompt', append_prompt]

  # host-mode bro skill surfacing: populate a per-session tmp dir and pass it
  # via `--add-dir` so claude's skill discovery picks up `<dir>/.claude/skills/`.
  # avoids the shared `<proj>/.claude/skills/` collision when multiple host
  # sessions run on the same repo — each `_populate_bro_skills` call wipes
  # foreign symlinks before recreating its own, which previously trampled
  # concurrent sessions. container mode keeps writing to the workspace's
  # `.claude/skills/` (single-session FS, no concurrency).
  if not container and bro_env is not None:
    skills_root = Path(tempfile.mkdtemp(prefix=f'cw-skills-{bro_env}-'))
    _populate_bro_skills(skills_root, bro_env)
    claude_args = [*claude_args, '--add-dir', str(skills_root)]

  if prompt is not None:
    claude_args = [*claude_args, '--', prompt]

  # scope credentials to the themed bro (dive-in sets CW_BRO=ppp-dev; a manual
  # `cw ss -c` defaults to it too). host mode resolves from ~/.ppp directly, so no
  # hydration there.
  secrets: set[str] = set()
  if container:
    bro_name = bro_env if bro_env is not None else _DEFAULT_CW_BRO
    secrets, _ = _container_secrets(bro_name, mcp=mcp, bro_mode=False)
    try:
      secrets = _finalize_secrets(secrets, grant=grant, revoke=revoke)
    except ValueError as e:
      log.error('%s', e)
      return 1
  return cw(
    name=name,
    container=container,
    drop=drop,
    auto=auto,
    base_ref=base_ref,
    claude_args=claude_args,
    secrets=secrets,
  )


_BRO_MCP_SERVER_NAME = 'bro'
# path inside the container; /host-repo is the host project bind mount (see
# _docker_create_argv). passed as claude code's apiKeyHelper so claude reads the
# api key from the `anthropic` secret without the "Detected a custom API key"
# prompt that ANTHROPIC_API_KEY would trigger.
_BRO_API_KEY_HELPER = '/host-repo/setup/print_anthropic_key.sh'


def _populate_bro_skills(proj: Path, bro_name: str) -> None:
  """populate <proj>/.claude/skills/<name>/SKILL.md as relative symlinks into the
  named bro's `bro/bros/<bro>/skills/<name>.md` files.

  two call sites:
   - container entrypoint (`cw populate-bro-skills $CW_BRO`) — `proj` is the
     workspace root; the container's ephemeral FS means no concurrency concerns.
   - host-mode `start_session` — `proj` is a per-session `tempfile.mkdtemp`
     directory passed to claude via `--add-dir`, so concurrent dive-in sessions
     on the same repo don't share `.claude/skills/`.

  cleanup is symlink-aware: any existing `<name>/SKILL.md` that's a symlink is
  removed (and its parent dir cleaned up if empty) before recreating. static
  skills (regular SKILL.md files) are left untouched.
  """
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  skills_dir = proj / '.claude' / 'skills'
  skills_dir.mkdir(parents=True, exist_ok=True)
  for child in skills_dir.iterdir():
    if not child.is_dir():
      continue
    skill_md = child / 'SKILL.md'
    if skill_md.is_symlink():
      skill_md.unlink()
      try:
        child.rmdir()
      except OSError:
        pass
  for name, src in bro.skills.items():
    target_dir = skills_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)
    link = target_dir / 'SKILL.md'
    rel = os.path.relpath(src, link.parent)
    link.symlink_to(rel)
    log.info('populated .claude/skills/%s/SKILL.md → %s', name, rel)


def _bro_claude_argv(bro_name: str) -> list[str]:
  """build the clean claude argv for `cw ss --bro <bro_name>`.

  resolves the bro to extract its system prompt (no shared/base prepend), wires
  its declared MCP servers + data sources through the `mcp-server bro:<name>`
  stdio shim, and uses `--bare` + `--strict-mcp-config` + `--tools ""` to start
  claude with no project/user CLAUDE.md, no host MCP servers, no built-in
  skills, and only the bro's MCP tools. supplies apiKeyHelper via `--settings`
  (flagSettings, not project/local) so claude executes it without a workspace
  trust gate.
  """
  from bro.registry import create_bro

  bro = create_bro(bro_name)
  mcp_config = json.dumps(
    {
      'mcpServers': {
        _BRO_MCP_SERVER_NAME: {'command': 'mcp-server', 'args': [f'bro:{bro_name}']},
      },
    },
    separators=(',', ':'),
  )
  settings = json.dumps({'apiKeyHelper': _BRO_API_KEY_HELPER}, separators=(',', ':'))
  return [
    '--bare',
    '--strict-mcp-config',
    '--mcp-config',
    mcp_config,
    '--settings',
    settings,
    '--system-prompt',
    bro.system_prompt,
    '--tools',
    '',
    '--allowed-tools',
    f'mcp__{_BRO_MCP_SERVER_NAME}__*',
  ]


def _finalize_secrets(secrets: set[str], *, grant: list[str], revoke: list[str]) -> set[str]:
  """layer the per-session `--grant` / `--revoke` overrides onto a computed scoped
  set. grant/revoke apply strictly — a grant/revoke that doesn't change the set
  raises `ValueError` (`credentials.apply_grant_revoke`)."""
  return credentials.apply_grant_revoke(secrets, grant=grant, revoke=revoke)


def _container_secrets(
  bro_name: str, *, mcp: Optional[str], bro_mode: bool
) -> tuple[set[str], bool]:
  """scoped credential set + docker-socket decision for a container session
  themed as `bro_name`. the two surfaces request different sets (hydration is
  strict, so each requests only what it actually uses):

  - `--bro` (`claude --bare` serving the bro's own in-process MCP servers): the
    bro's full `needed_secrets()` + `anthropic` for the apiKeyHelper. docker
    socket only if `bro.needs_docker`.
  - a native claude code session themed as the bro (dive-in / plain `cw ss`): it
    drives the bro's *skills* (bash → `extra_secrets`) and its flow via `--mcp`,
    not the bro's in-process MCP / data-source toolset — so only `extra_secrets`
    + `flow_mcp` (when `--mcp http`). always keeps the socket (it has a Bash tool).

  both add the session baseline (sync-log + trails).
  """
  from bro.registry import create_bro

  secrets: set[str] = set(_CW_SESSION_BASELINE)
  docker_sock = True
  try:
    bro = create_bro(bro_name)
  except KeyError as e:
    # unknown bro (registry KeyError) only — other failures propagate rather than
    # collapse into a silently under-scoped session. a native session still gets
    # the socket; a --bro fallback does not (moot anyway — _bro_claude_argv
    # re-raises the same KeyError downstream).
    log.warning('could not resolve bro %r for credential scoping: %s', bro_name, e)
    return secrets, not bro_mode
  if bro_mode:
    secrets.update(bro.needed_secrets())
    secrets.add('anthropic')
    docker_sock = bro.needs_docker
  else:
    secrets.update(bro._extra_secrets)
    if mcp == 'http':
      secrets.add('flow_mcp')
  return secrets, docker_sock


def _replace_container_resume_hint(name: str) -> None:
  """overwrite claude's misleading `claude --resume <id>` hint with a host-side one.

  claude prints a two-line resume hint on exit, but the `claude --resume <id>`
  command it suggests only works inside the container — the session jsonl
  lives at ~/.claude/cw-sessions/<name>/projects/-workspace/ on the host, not
  where a bare host-side `claude` would look. We replace it with the
  cw-side resume command that actually works.

  Only meaningful when stdout is a TTY (otherwise the ANSI escape is junk in
  a log) and a session jsonl exists (otherwise claude didn't print a hint).
  """
  if not sys.stdout.isatty():
    return
  if _latest_jsonl(_projects_dir_for_container(name)) is None:
    return
  # \033[2A: move cursor up 2 lines (over claude's hint).
  # \033[J:  clear from cursor to end of screen.
  sys.stdout.write('\033[2A\033[J')
  print('Resume this session with:')
  print(f'  cw ss -c --mcp --resume {name}')


def _ppp_tarball(files: dict[str, bytes]) -> bytes:
  """pack a scoped credential store into a tar for `docker cp` into /home/cw.

  entries are prefixed `.ppp/` so extracting at /home/cw lands them at
  /home/cw/.ppp/<file>. files are 0600, the dir 0700, all owned by the host
  uid/gid (the same uid the entrypoint remaps `cw` to on Linux); the entrypoint
  re-owns the tree to `cw` after its remap so the bytes are readable there and on
  Docker for Mac (where the remap is skipped). mtime defaults to 0 — deterministic,
  no clock needed.
  """
  uid, gid = os.getuid(), os.getgid()
  buf = io.BytesIO()
  with tarfile.open(fileobj=buf, mode='w') as tar:
    root = tarfile.TarInfo('.ppp')
    root.type = tarfile.DIRTYPE
    root.mode = 0o700
    root.uid, root.gid = uid, gid
    tar.addfile(root)
    for fname in sorted(files):
      data = files[fname]
      info = tarfile.TarInfo(f'.ppp/{fname}')
      info.size = len(data)
      info.mode = 0o600
      info.uid, info.gid = uid, gid
      tar.addfile(info, io.BytesIO(data))
  return buf.getvalue()


def run_in_container(
  name: str,
  command: list[str],
  *,
  drop: bool = False,
  secrets: Collection[str] = (),
  docker_sock: bool = True,
  extra_env: Optional[Mapping[str, str]] = None,
  forward_bro: bool = True,
) -> int:
  """run `command` inside a fresh cw-style container backed by workspace `name`.

  builds/reuses the image, creates `var/cw/containers/<name>/`, runs the container
  (`docker create` + `docker cp` scoped secrets in + `docker start -a -i`, the
  run-equivalent split that lets us inject the store into the pre-start container)
  with the standard bind mounts (`/workspace`, `/host-repo:ro`, `.claude` overlay,
  optionally the docker socket, …), then post-syncs OAuth credentials. When
  `drop=True`, removes the workspace dir and per-session claude state on exit.
  Returns the container's exit code.

  `secrets` is the scoped credential set hydrated into the container's ~/.ppp
  (see `credentials.build_scoped_store`); a missing secret raises (strict). AWS is
  just one of them (`aws`), wired in by its install hook. `docker_sock=False`
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
  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  # build the scoped store in memory (strict: a missing secret raises before the
  # container is created), then inject it into the pre-start container's writable
  # layer via `docker cp`. nothing plaintext touches the host disk.
  store = credentials.build_scoped_store(secrets)
  names = sorted(set(secrets))
  log.info('scoped secrets for %s: %s', name, ', '.join(names) if len(names) > 0 else '(none)')
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
  # post-run sync: if the container refreshed its OAuth token during the
  # session, propagate the fresher copy back to the host so the next session
  # (host or container) sees the live tokens.
  _sync_credentials(claude_dir / '.credentials.json', home / '.claude' / '.credentials.json')
  if drop:
    try:
      _remove_container_dir(session, _cleanup_image())
      log.info('removed container workspace %s', name)
    except RuntimeError as e:
      log.warning('could not fully remove container workspace %s: %s', name, e)
    if claude_dir.is_dir():
      shutil.rmtree(claude_dir, ignore_errors=True)
  return result.returncode


def _ensure_host_worktree(worktree: Path, branch: str, base_ref: Optional[str] = None) -> bool:
  # create the worktree if new (git ops run in the project root, the cwd): a
  # `worktree-<name>` branch — based on base_ref (`--into`) when given, else the
  # current HEAD — plus submodule alternates so `git submodule update` reuses the
  # superproject's modules. an already-existing branch defines its own base, so
  # base_ref doesn't apply there.
  if worktree.is_dir():
    return True
  log.info('creating worktree %s', worktree)
  branch_exists = (
    subprocess.run(
      ['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], capture_output=True
    ).returncode
    == 0
  )
  if branch_exists:
    add = ['git', 'worktree', 'add', str(worktree), branch]
  else:
    base = [base_ref] if base_ref is not None else []
    add = ['git', 'worktree', 'add', str(worktree), '-b', branch, *base]
  if subprocess.run(add).returncode != 0:
    log.error('failed to create worktree %s', worktree)
    return False
  for key, value in (
    ('submodule.alternateLocation', 'superproject'),
    ('submodule.alternateErrorStrategy', 'info'),
  ):
    subprocess.run(['git', '-C', str(worktree), 'config', key, value], check=False)
  return True


def _provision_host_worktree(worktree: Path) -> bool:
  # run the worktree's own provision_repo.sh against itself (idempotent: skips the
  # uv sync when the venv is current, always refreshes the console-script bridge +
  # git hooks). shared with host setup_repo.sh and the container entrypoint.
  script = worktree / 'setup' / 'provision_repo.sh'
  if not script.is_file():
    log.warning('%s not found (worktree on an old base?); skipping provisioning', script)
    return True
  if subprocess.run([str(script)], cwd=str(worktree)).returncode != 0:
    log.error('failed to provision worktree %s', worktree)
    return False
  return True


def _remove_host_worktree(worktree: Path, branch: str) -> None:
  subprocess.run(
    ['git', 'worktree', 'remove', '--force', str(worktree)], check=False, capture_output=True
  )
  subprocess.run(['git', 'branch', '-D', branch], check=False, capture_output=True)


def _finish_host_worktree(name: str, worktree: Path, branch: str, *, interactive: bool) -> None:
  # on exit, warn if the worktree isn't landed on origin/master, then (interactive
  # only) offer to drop it. non-interactive sessions keep it — safe default, and the
  # path stays correct if --auto/--bro ever run on host. `cw clean` removes it later.
  _, reasons = _worktree_is_clean(worktree)
  if len(reasons) > 0:
    log.warning('worktree %s not landed on origin/master:', name)
    for reason in reasons:
      log.warning('  - %s', reason)
  if not interactive:
    return
  if yesno(f'drop worktree {name}?'):
    _remove_host_worktree(worktree, branch)


def cw(
  name: str,
  container: bool,
  drop: bool,
  claude_args: list[str],
  *,
  auto: bool = False,
  base_ref: Optional[str] = None,
  secrets: Collection[str] = (),
  docker_sock: bool = True,
) -> int:
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    log.info('already inside a container; falling back to host mode')
    container = False

  if container:
    # the entrypoint reads CW_BASE_REF to base the fresh clone's worktree branch
    # (the sha's objects are already shared from /host-repo via clone alternates)
    extra_env = {'CW_BASE_REF': base_ref} if base_ref is not None else None
    code = run_in_container(
      name,
      ['claude', *claude_args],
      drop=drop,
      secrets=secrets,
      docker_sock=docker_sock,
      extra_env=extra_env,
    )
    if not drop and code == 0:
      _replace_container_resume_hint(name)
    return code

  # host mode: cw owns the worktree lifecycle (create + provision + cleanup) and
  # launches plain `claude` from inside it — no `claude -w`, so no claude provisioning
  # hooks. provisioning is the same provision_repo.sh the container entrypoint runs.
  proj = _project_root()
  os.chdir(proj)
  worktree = _worktrees_dir(proj) / name
  branch = f'worktree-{name}'

  if not _ensure_host_worktree(worktree, branch, base_ref):
    return 1
  if not _provision_host_worktree(worktree):
    return 1

  env = _venv_env(worktree / '.venv')
  pidfile = _host_pidfile(proj, name)
  pidfile.parent.mkdir(parents=True, exist_ok=True)
  pidfile.write_text(str(os.getpid()))
  try:
    result = subprocess.run(['claude', *claude_args], cwd=str(worktree), env=env)
  finally:
    pidfile.unlink(missing_ok=True)

  if drop:
    _remove_host_worktree(worktree, branch)
  else:
    _finish_host_worktree(name, worktree, branch, interactive=not auto and sys.stdin.isatty())
  return result.returncode


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='launch claude with worktree management')
  subparsers = parser.add_subparsers(dest='cmd', required=True)

  ss = subparsers.add_parser('ss', help='start a claude session in a worktree')
  ss.add_argument(
    '-c', '--container', action='store_true', help='run claude inside an isolated docker container'
  )
  ss.add_argument(
    '--drop', action='store_true', help='remove the workspace on exit without prompting'
  )
  add_forwarded_flags(ss)
  ss.add_argument(
    '--mcp',
    nargs='?',
    const='http',
    default=None,
    choices=['http', 'local'],
    help='connect flow MCP tools: http (default) uses the deployed server, local spawns a stdio process',
  )
  ss.add_argument(
    '--bro',
    default=None,
    help="start a clean claude session with the named bro's persona (system prompt, MCP servers, tools); requires -c and the `anthropic` secret; mutually exclusive with --mcp, --auto, --resume",
  )
  ss.add_argument(
    '-p', '--prompt', default=None, help='initial prompt (prepended with base prompt)'
  )
  ss.add_argument('name', help='worktree name')
  ss.add_argument('claude_args', nargs=REMAINDER, help='args forwarded to claude')

  subparsers.add_parser('list', help='list workspaces ([.]=local, [o]=container, [x]=abandoned)')

  clean = subparsers.add_parser(
    'clean', help='remove stale workspaces that have no uncommitted or unpushed changes'
  )
  clean.add_argument(
    '--force',
    action='store_true',
    help='remove workspaces even if they have uncommitted or unpushed changes',
  )
  clean.add_argument(
    '--dry-run',
    action='store_true',
    help='show what would be removed without actually removing',
  )
  clean.add_argument(
    'refs',
    nargs='*',
    help='workspaces to clean (default: all); use c:<name> for container workspaces',
  )

  check_clean = subparsers.add_parser(
    'check-clean',
    help='check if a workspace is clean (exit 0=clean, 1=not); reasons printed to stderr',
  )
  check_clean.add_argument(
    'ref',
    nargs='?',
    help='workspace to check (default: cwd); use c:<name> for container workspaces',
  )

  exec_cmd = subparsers.add_parser(
    'exec',
    help='exec a command in the running container for a workspace (default: interactive bash with .venv activated)',
  )
  exec_cmd.add_argument(
    'name', help="container workspace name (the 'c:' prefix is accepted but optional)"
  )
  exec_cmd.add_argument('command', nargs=REMAINDER, help='command + args to exec (default: bash)')

  populate = subparsers.add_parser(
    'populate-bro-skills',
    help="symlink the named bro's skills into .claude/skills/ for Claude Code slash-command discovery (run from the --bro container entrypoint)",
  )
  populate.add_argument('bro_name', help='registered bro name (e.g. ppp-dev)')

  banner_parser = subparsers.add_parser(
    'banner',
    help='print the banner; auto-run by the container .bashrc on `cw exec` shells',
  )
  banner_parser.add_argument(
    '--llm',
    action='store_true',
    help='emit plain key:value lines for LLM Bash-tool consumption (no ANSI, no logo)',
  )

  args = parser.parse(argv)
  cmd = args.pop('cmd')

  if cmd == 'list':
    return list_workspaces()
  if cmd == 'clean':
    return clean_workspaces(force=args['force'], dry_run=args['dry_run'], refs=args['refs'])
  if cmd == 'check-clean':
    ref = args['ref']
    if ref is None:
      target, container_proj = Path.cwd(), None
    else:
      try:
        target, container_proj = _resolve_workspace(ref, _project_root())
      except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    clean_, reasons = _worktree_is_clean(target, container_proj=container_proj)
    for r in reasons:
      print(r, file=sys.stderr)
    return 0 if clean_ else 1
  if cmd == 'exec':
    return exec_in_workspace(name=args['name'], cmd=args['command'])
  if cmd == 'populate-bro-skills':
    _populate_bro_skills(_project_root(), args['bro_name'])
    return 0
  if cmd == 'banner':
    return banner(llm=args['llm'])
  assert cmd == 'ss'
  if args['auto'] and not args['container']:
    parser.error('--auto requires --container')
  if args['into'] is not None and args['resume']:
    parser.error(
      '--into cannot be combined with --resume (it only applies when creating a workspace)'
    )
  if args['bro'] is not None:
    if not args['container']:
      parser.error('--bro requires --container')
    if args['auto']:
      parser.error('--bro cannot be combined with --auto')
    if args['mcp'] is not None:
      parser.error('--bro cannot be combined with --mcp (the bro defines its own MCP servers)')
    if args['resume']:
      parser.error('--bro cannot be combined with --resume')
    if _load_anthropic_key() is None:
      parser.error(
        '--bro requires the `anthropic` secret to provide an api_key '
        '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
      )
  if args['resume']:
    if args['drop']:
      parser.error('--resume cannot be combined with --drop')
    if args['prompt'] is not None:
      parser.error(
        '--resume cannot be combined with -p/--prompt (the initial prompt is ignored on resume)'
      )
  if (args['grant'] is not None or args['revoke'] is not None) and not args['container']:
    parser.error(
      '--grant/--revoke require -c/--container: host mode is unscoped, so a revoke '
      'could not actually restrict the session'
    )
  return start_session(**args)
