#!/usr/bin/env python
"""launch claude, optionally in an isolated docker container.

host mode (default): execs `claude -w <name>` from the project root. Claude's
`-w` flag owns the worktree lifecycle (create, keep/drop prompt, cleanup); our
.claude/hooks/worktree_create.sh WorktreeCreate hook applies project-specific
provisioning (branch naming, CLAUDE_BASE marker, submodule alternateLocation).

container mode (--container): /workspace is a fresh clone, not a worktree — the
gitfile-based worktree layout doesn't survive the container boundary, and this
keeps the container's git state genuinely isolated. layout:

  - var/cw/containers/<name>/ on the host → /workspace rw
    (empty on first run; entrypoint clones host repo into it)
  - host project root → /host-repo ro
    (clone --shared reads objects from here via alternates; also the source for
    local submodule clones to avoid needing ssh keys in the container)
  - ~/.claude.json: not bind-mounted from host. Seeded once per workspace from
    host into a container-private file at cw-sessions/<name>/.claude.json and
    bind-mounted from there, so per-project state mutations (mcpServers,
    allowedTools, hasTrustDialogAccepted) stay in the container and can't be
    used to escalate to code execution in the next host claude session.
  - ~/.claude → /host-claude ro (seeded once into the container-private
    ~/.claude/cw-sessions/<name>/, minus sessions/projects/history)
  - ~/.claude/.credentials.json: not bind-mounted from host. Seeded into
    cw-sessions/<name>/.credentials.json before launch (if host is fresher)
    and synced back to host on exit (if container is fresher), keyed on
    claudeAiOauth.expiresAt. Removes the runtime token-swap vector while
    preserving OAuth refresh.
  - .configs/cw_github_token → /run/secrets/github_token ro (when present;
    entrypoint configures git credential helper for https push)

network is not restricted by design.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import humanize

import configs
from base import log
from base.args import Parser

CONTAINER_DIR = Path(__file__).resolve().parent / 'setup' / 'container'
_PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'
_BASE_PROMPT_DIRS = ['shared', 'base']


def _load_base_prompts() -> str:
  parts = []
  for subdir in _BASE_PROMPT_DIRS:
    for p in sorted((_PROMPTS_DIR / subdir).glob('*')):
      if p.is_file():
        parts.append(p.read_text().strip())
  return '\n\n'.join(parts)


_DOCKER_FORWARD_ENV = (
  'CW_BRO',
  'CW_COMMAND',
  'CW_TASK_ID',
  'CW_TOKEN_FILE',
  'GITHUB_TOKEN',
  'GIT_AUTHOR_NAME',
  'GIT_AUTHOR_EMAIL',
  'GIT_COMMITTER_NAME',
  'GIT_COMMITTER_EMAIL',
  'PPP_SHELL_COMMAND',
  'TERM_PROGRAM',
  'TERM_PROGRAM_VERSION',
  'COLORTERM',
  'VTE_VERSION',
)
_DOCKER_AWS_ENV = (
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'AWS_DEFAULT_REGION',
  'AWS_REGION',
  'AWS_PROFILE',
)

_BRO_GIT_NAME = 'Bro'
_BRO_GIT_EMAIL = 'dzhioev+bro@gmail.com'
_BRO_TOKEN_FILE = 'cw_github_token_bro'
_USER_TOKEN_FILE = 'cw_github_token'
_ANTHROPIC_CONFIG_PATH = Path(configs.DEFAULT_CONFIGS_DIR) / 'anthropic.json'


def _load_anthropic_key() -> str | None:
  """return the api_key from anthropic.json, or None if missing/invalid."""
  if not _ANTHROPIC_CONFIG_PATH.is_file():
    return None
  key = json.loads(_ANTHROPIC_CONFIG_PATH.read_text()).get('api_key')
  if not isinstance(key, str) or len(key) == 0:
    return None
  return key


def _venv_env(venv: Path) -> dict[str, str]:
  env = {**os.environ, 'VIRTUAL_ENV': str(venv)}
  env['PATH'] = str(venv / 'bin') + ':' + env.get('PATH', '')
  env.pop('PYTHONHOME', None)
  return env


def _git_out(*args: str, cwd: str | None = None) -> str:
  return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


def _project_root() -> Path:
  return Path(_git_out('rev-parse', '--git-common-dir')).resolve().parent


def _keychain_credentials() -> dict | None:
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


def _seed_container_claude_json(claude_dir: Path, host_file: Path) -> Path:
  """seed-once per-workspace container-private copy of ~/.claude.json.

  if the seed doesn't exist yet, copy host's ~/.claude.json into it (or write
  an empty JSON object if the host has no such file). subsequent runs keep
  whatever the container last wrote. returns the seed path; caller bind-mounts
  it to /home/cw/.claude.json.
  """
  seed = claude_dir / '.claude.json'
  if not seed.exists():
    if host_file.is_file():
      shutil.copyfile(host_file, seed)
    else:
      seed.write_text('{}')
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


def _docker_run_argv(
  tag: str, name: str, proj: Path, session: Path, command: list[str], *, aws: bool = False
) -> list[str]:
  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  claude_dir.mkdir(parents=True, exist_ok=True)
  # seed-once container-private ~/.claude.json (see module docstring)
  claude_json = _seed_container_claude_json(claude_dir, home / '.claude.json')
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
    'run',
    '-it',
    '--rm',
    '-v',
    f'{session}:/workspace',
    '-v',
    f'{proj}:/host-repo:ro',
    '-v',
    f'{claude_json}:/home/cw/.claude.json',
    '-v',
    f'{home}/.claude:/host-claude:ro',
    '-v',
    f'{claude_dir}:/home/cw/.claude',
    '-v',
    f'{home}/.claude/settings.json:/home/cw/.claude/settings.json:ro',
    '-v',
    f'{home}/.gitconfig:/host-gitconfig:ro',
    '-e',
    'HOME=/home/cw',
    '-e',
    f'CW_NAME={name}',
    '-e',
    'DISABLE_AUTOUPDATER=1',
    '-e',
    'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1',
    '-w',
    '/workspace',
    '--memory=8g',
    # bind-mount the host docker socket so deploy scripts inside the container
    # can `docker build` / `docker push` against the host daemon (no nested
    # runtime). gives an in-container process API-level control over host
    # docker, which is a real escalation vector but bounded — the alternative
    # (rootless podman + privileged-equivalent flags) has the same blast
    # radius across more attack surfaces. cw is single-user dev.
    '-v',
    '/var/run/docker.sock:/var/run/docker.sock',
  ]
  for var in _DOCKER_FORWARD_ENV:
    if os.environ.get(var) is not None:
      argv += ['-e', var]
  github_token = (proj / '.configs' / os.environ.get('CW_TOKEN_FILE', _USER_TOKEN_FILE)).resolve()
  if github_token.is_file():
    argv += ['-v', f'{github_token}:/run/secrets/github_token:ro']
  if aws:
    host_aws = home / '.aws'
    if host_aws.is_dir():
      argv += ['-v', f'{host_aws}:/home/cw/.aws:ro']
    for var in _DOCKER_AWS_ENV:
      if os.environ.get(var) is not None:
        argv += ['-e', var]
  return [*argv, tag, *command]


def _is_local_active(name: str) -> bool:
  result = subprocess.run(['pgrep', '-f', f'claude -w {name}'], capture_output=True)
  return result.returncode == 0


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


def _latest_jsonl(projects_dir: Path) -> Path | None:
  if not projects_dir.is_dir():
    return None
  jsonls = [p for p in projects_dir.iterdir() if p.suffix == '.jsonl']
  if len(jsonls) == 0:
    return None
  return max(jsonls, key=lambda p: p.stat().st_mtime)


def _projects_dir_for_local(name: str, proj: Path) -> Path:
  worktree = proj / '.claude' / 'worktrees' / name
  encoded = str(worktree).replace('/', '-').replace('.', '-')
  return Path.home() / '.claude' / 'projects' / encoded


def _projects_dir_for_container(name: str) -> Path:
  return Path.home() / '.claude' / 'cw-sessions' / name / 'projects' / '-workspace'


def _read_subject(projects_dir: Path) -> str | None:
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
      text: str | None = None
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


def _last_active(worktree: Path) -> float | None:
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


def _format_ref(name: str, is_container: bool) -> str:
  return f'{_CONTAINER_PREFIX}{name}' if is_container else name


def _parse_ref(ref: str) -> tuple[str, bool]:
  if ref.startswith(_CONTAINER_PREFIX):
    return ref[len(_CONTAINER_PREFIX) :], True
  return ref, False


def _find_container_id(name: str, proj: Path) -> str | None:
  """find the running container backing the named container workspace.

  filters `docker ps` by the workspace's host mount path, which is unique per
  workspace. returns the container short id, or None if no running container
  is bound to that mount.
  """
  session = proj / 'var' / 'cw' / 'containers' / name
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
  return subprocess.run(['docker', 'exec', '-it', container_id, *docker_cmd]).returncode


def _resolve_workspace(ref: str, proj: Path) -> tuple[Path, Path | None]:
  name, is_container = _parse_ref(ref)
  if is_container:
    path = proj / 'var' / 'cw' / 'containers' / name
    if not path.is_dir():
      raise ValueError(f'container workspace not found: {ref}')
    return path, proj
  path = proj / '.claude' / 'worktrees' / name
  if not path.is_dir():
    raise ValueError(f'workspace not found: {ref}')
  return path, None


def _list_entry_local(p: Path, proj: Path) -> tuple[str, bool, str, str | None, float | None]:
  kind = 'L' if _is_local_active(p.name) else 'X'
  pdir = _projects_dir_for_local(p.name, proj)
  return (kind, False, p.name, _read_subject(pdir), _last_active(p))


def _list_entry_container(
  p: Path, mounts: set[str]
) -> tuple[str, bool, str, str | None, float | None]:
  kind = 'C' if str(p) in mounts else 'X'
  pdir = _projects_dir_for_container(p.name)
  return (kind, True, p.name, _read_subject(pdir), _last_active(p))


def list_workspaces() -> int:
  proj = _project_root()
  worktrees_dir = proj / '.claude' / 'worktrees'
  containers_dir = proj / 'var' / 'cw' / 'containers'

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


def _worktree_is_clean(path: Path, container_proj: Path | None = None) -> tuple[bool, list[str]]:
  """check whether a worktree is safe to remove.

  returns (safe, reasons) where reasons lists what prevents removal.
  container_proj: when set, `path` is a container clone whose own remotes
  are unreachable from the host (origin = HTTPS GitHub without creds, host
  remote = /host-repo bind mount). Ancestry checks run in container_proj
  (resp. container_proj/<sub_path>) instead, with the container's HEAD
  fetched in first; container_proj/.git/objects is also exposed as an
  alternate so basic git ops in the container clone can resolve their
  /host-repo alternates.
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
  fetch = subprocess.run(
    ['git', 'fetch', '--quiet', 'origin', 'master'],
    cwd=check_root,
    capture_output=True,
    env=no_prompt_env,
  )
  if fetch.returncode != 0:
    reasons.append('could not fetch origin/master')
  else:
    head_ref = 'HEAD'
    if container_proj is not None:
      bring_in = subprocess.run(
        ['git', 'fetch', '--quiet', str(path), 'HEAD'],
        cwd=check_root,
        capture_output=True,
        env=no_prompt_env,
      )
      if bring_in.returncode != 0:
        reasons.append("could not fetch container's HEAD")
        head_ref = None
      else:
        head_ref = 'FETCH_HEAD'
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
          env=no_prompt_env,
        )
        if ancestor.returncode != 0:
          ahead = subprocess.run(
            ['git', 'rev-list', '--count', head_ref, '^origin/master'],
            cwd=check_root,
            capture_output=True,
            text=True,
            env=no_prompt_env,
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


def clean_workspaces(
  force: bool = False, dry_run: bool = False, refs: list[str] | None = None
) -> int:
  proj = _project_root()
  worktrees_dir = proj / '.claude' / 'worktrees'
  containers_dir = proj / 'var' / 'cw' / 'containers'

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

  def _check_local(p: Path) -> tuple[Path, bool, bool, list[str]]:
    if _is_local_active(p.name):
      return p, True, False, []
    safe, reasons = _worktree_is_clean(p)
    return p, False, safe, reasons

  def _check_container(p: Path, mounts: set[str]) -> tuple[Path, bool, bool, list[str]]:
    if str(p) in mounts:
      return p, True, False, []
    safe, reasons = _worktree_is_clean(p, container_proj=proj)
    return p, False, safe, reasons

  with concurrent.futures.ThreadPoolExecutor() as pool:
    mounts_future = pool.submit(_running_container_mounts) if len(container_dirs) > 0 else None
    local_results = list(pool.map(_check_local, local_dirs))
    mounts = mounts_future.result() if mounts_future is not None else set()
    container_results = list(pool.map(lambda p: _check_container(p, mounts), container_dirs))

  removed = 0
  skipped = 0

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
      shutil.rmtree(p)
      session_dir = Path.home() / '.claude' / 'cw-sessions' / p.name
      if session_dir.is_dir():
        shutil.rmtree(session_dir)
      log.info('removed %s', ref)
    removed += 1

  log.info('cleaned %d workspace(s), skipped %d', removed, skipped)
  return 0


def _mcp_config_argv(mcp: str) -> list[str]:
  if mcp == 'local':
    return ['--mcp-config=flow/mcp/mcp_local.json']
  assert mcp == 'http'
  config_path = _project_root() / '.configs' / 'flow_mcp.json'
  if not config_path.is_file():
    raise SystemExit(f'missing {config_path} — run flow/mcp/server/bootstrap_secrets.sh')
  cfg = json.loads(config_path.read_text())
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


def add_forwarded_flags(parser: argparse.ArgumentParser) -> None:
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
    '--aws',
    action='store_true',
    help='expose host AWS credentials (~/.aws and env vars) to the container',
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
  aws: bool,
  effort: str | None,
  rc: bool,
  resume: bool,
  mcp: str | None,
  bro: str | None,
  prompt: str | None,
  claude_args: list[str],
) -> int:
  rc = rc or auto
  flags = {
    '-c': container,
    '--auto': auto,
    '--drop': drop,
    '--aws': aws,
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
  parts.extend([name, *claude_args])
  os.environ['CW_COMMAND'] = ' '.join(parts)
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

  if auto:
    os.environ['GIT_AUTHOR_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_AUTHOR_EMAIL'] = _BRO_GIT_EMAIL
    os.environ['GIT_COMMITTER_NAME'] = _BRO_GIT_NAME
    os.environ['GIT_COMMITTER_EMAIL'] = _BRO_GIT_EMAIL
    os.environ['CW_TOKEN_FILE'] = _BRO_TOKEN_FILE
    proj = _project_root()
    token_path = (proj / '.configs' / _BRO_TOKEN_FILE).resolve()
    if token_path.is_file():
      os.environ['GITHUB_TOKEN'] = token_path.read_text().strip()

  if bro is not None:
    # the container entrypoint reads CW_BRO and runs `cw populate-bro-skills`
    # after venv activation, so claude code's slash-command discovery picks up
    # the bro's skills from .claude/skills/<name>/SKILL.md symlinks.
    os.environ['CW_BRO'] = bro
    claude_args = [*_bro_claude_argv(bro), *claude_args]
    if prompt is not None:
      claude_args = [*claude_args, '--', prompt]
    return cw(name=name, container=container, drop=drop, aws=aws, claude_args=claude_args)

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

  prompt_parts = [_load_base_prompts()]
  if auto:
    prompt_parts.append('Land mode: PR')
  claude_args = [*claude_args, '--append-system-prompt', '\n\n'.join(prompt_parts)]
  if prompt is not None:
    claude_args = [*claude_args, '--', prompt]

  return cw(name=name, container=container, drop=drop, aws=aws, claude_args=claude_args)


_BRO_MCP_SERVER_NAME = 'bro'
# path inside the container; /host-repo is the host project bind mount (see
# _docker_run_argv). passed as claude code's apiKeyHelper so claude reads the
# api key from .configs/anthropic.json without the "Detected a custom API key"
# prompt that ANTHROPIC_API_KEY would trigger.
_BRO_API_KEY_HELPER = '/host-repo/setup/print_anthropic_key.sh'


def _populate_bro_skills(proj: Path, bro_name: str) -> None:
  """populate <proj>/.claude/skills/<name>/SKILL.md as relative symlinks into the
  named bro's `bro/bros/<bro>/skills/<name>.md` files. used by `cw ss --bro`
  to surface a bro's skills to Claude Code's slash-command discovery — `--bare`
  keeps skill resolution working, so populated symlinks are picked up by
  `/skill-name` typed in chat.

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


def run_in_container(
  name: str, command: list[str], *, aws: bool = False, drop: bool = False
) -> int:
  """run `command` inside a fresh cw-style container backed by workspace `name`.

  builds/reuses the image, creates `var/cw/containers/<name>/`, runs `docker run
  -it --rm` with the standard bind mounts (`/workspace`, `/host-repo:ro`,
  `.claude` overlay, docker socket, …), then post-syncs OAuth credentials. When
  `drop=True`, removes the workspace dir and per-session claude state on exit.
  Returns the container's exit code.
  """
  proj = _project_root()
  session = proj / 'var' / 'cw' / 'containers' / name
  session.mkdir(parents=True, exist_ok=True)
  tag = _image_tag()
  _ensure_image(tag)
  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  result = subprocess.run(_docker_run_argv(tag, name, proj, session, command, aws=aws))
  # post-run sync: if the container refreshed its OAuth token during the
  # session, propagate the fresher copy back to the host so the next session
  # (host or container) sees the live tokens.
  _sync_credentials(claude_dir / '.credentials.json', home / '.claude' / '.credentials.json')
  if drop:
    shutil.rmtree(session, ignore_errors=True)
    if claude_dir.is_dir():
      shutil.rmtree(claude_dir, ignore_errors=True)
    log.info('removed container workspace %s', name)
  return result.returncode


def cw(name: str, container: bool, drop: bool, aws: bool, claude_args: list[str]) -> int:
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    log.info('already inside a container; falling back to host mode')
    container = False

  if container:
    code = run_in_container(name, ['claude', *claude_args], aws=aws, drop=drop)
    if not drop and code == 0:
      _replace_container_resume_hint(name)
    return code

  proj = _project_root()
  os.chdir(proj)

  env = _venv_env(proj / '.claude' / 'worktrees' / name / '.venv')

  if not drop:
    os.execvpe('claude', ['claude', '-w', name, *claude_args], env)

  env['CW_DROP'] = '1'
  result = subprocess.run(['claude', '-w', name, *claude_args], env=env)
  worktree = proj / '.claude' / 'worktrees' / name
  subprocess.run(
    ['git', 'worktree', 'remove', '--force', str(worktree)], check=False, capture_output=True
  )
  subprocess.run(['git', 'branch', '-D', f'worktree-{name}'], check=False, capture_output=True)
  return result.returncode


def main(argv=None):
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
    help="start a clean claude session with the named bro's persona (system prompt, MCP servers, tools); requires -c and .configs/anthropic.json; mutually exclusive with --mcp, --auto, --resume",
  )
  ss.add_argument(
    '-p', '--prompt', default=None, help='initial prompt (prepended with base prompt)'
  )
  ss.add_argument('name', help='worktree name')
  ss.add_argument('claude_args', nargs=argparse.REMAINDER, help='args forwarded to claude')

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
  exec_cmd.add_argument(
    'command', nargs=argparse.REMAINDER, help='command + args to exec (default: bash)'
  )

  populate = subparsers.add_parser(
    'populate-bro-skills',
    help="symlink the named bro's skills into .claude/skills/ for Claude Code slash-command discovery (run from the --bro container entrypoint)",
  )
  populate.add_argument('bro_name', help='registered bro name (e.g. ppp-dev)')

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
  assert cmd == 'ss'
  if args['auto'] and not args['container']:
    parser.error('--auto requires --container')
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
        f'--bro requires an Anthropic API key at {_ANTHROPIC_CONFIG_PATH} '
        '({"api_key": "..."}); claude --bare does not use OAuth or keychain'
      )
  if args['resume']:
    if args['drop']:
      parser.error('--resume cannot be combined with --drop')
    if args['prompt'] is not None:
      parser.error(
        '--resume cannot be combined with -p/--prompt (the initial prompt is ignored on resume)'
      )
  if args['aws']:
    has_aws_dir = (Path.home() / '.aws').is_dir()
    has_aws_env = any(os.environ.get(v) is not None for v in _DOCKER_AWS_ENV)
    if not has_aws_dir and not has_aws_env:
      parser.error('--aws: no AWS credentials found (~/.aws missing and no AWS env vars set)')
  return start_session(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
