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
  - ~/.claude.json rw (auth tokens; refresh writes back to host)
  - ~/.claude → /host-claude ro (seeded once into the container-private
    ~/.claude/cw-sessions/<name>/, minus sessions/projects/history)
  - .configs/cw_github_token → /run/secrets/github_token ro (when present;
    entrypoint configures git credential helper for https push)

network is not restricted by design.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import humanize

from base import log
from base.args import Parser

CONTAINER_DIR = Path(__file__).resolve().parent / '.claude' / 'container'


def _git_out(*args: str, cwd: str | None = None) -> str:
  return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


def _project_root() -> Path:
  return Path(_git_out('rev-parse', '--git-common-dir')).resolve().parent


def _keychain_credentials() -> dict | None:
  if sys.platform != 'darwin':
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
  log.info('building %s', tag)
  subprocess.run(
    [
      'docker',
      'build',
      '-t',
      tag,
      '-f',
      str(CONTAINER_DIR / 'Dockerfile'),
      '--build-context',
      f'proj={_project_root()}',
      str(CONTAINER_DIR),
    ],
    check=True,
  )


def _docker_run_argv(
  tag: str, name: str, proj: Path, session: Path, claude_args: list[str]
) -> list[str]:
  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  claude_dir.mkdir(parents=True, exist_ok=True)
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
    f'{home}/.claude.json:/home/cw/.claude.json',
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
    '-w',
    '/workspace',
  ]
  # forward terminal capability vars so claude's markdown renderer detects
  # hyperlink support the same way it does on the host (OSC 8 rendering of
  # `[text](url)` otherwise falls back to raw markdown inside the container)
  for var in ('TERM_PROGRAM', 'TERM_PROGRAM_VERSION', 'COLORTERM', 'VTE_VERSION'):
    if os.environ.get(var) is not None:
      argv += ['-e', var]
  github_token = (proj / '.configs' / 'cw_github_token').resolve()
  if github_token.is_file():
    argv += ['-v', f'{github_token}:/run/secrets/github_token:ro']
  # bind-mount the host credentials file rw so token refreshes inside the
  # container propagate back to the host; without this, each container consumes
  # the single-use OAuth refresh token and the host copy becomes stale, forcing
  # re-login on the next session.
  # on macOS: the keychain may have fresher tokens (e.g. after a host-mode login
  # that updated the keychain but not the file) — compare expiresAt and pick the
  # more recent source.
  host_creds = home / '.claude' / '.credentials.json'
  keychain_creds = _keychain_credentials()
  if keychain_creds is not None:
    keychain_expiry = keychain_creds.get('claudeAiOauth', {}).get('expiresAt', 0)
    file_expiry = 0
    if host_creds.is_file():
      try:
        file_expiry = (
          json.loads(host_creds.read_text()).get('claudeAiOauth', {}).get('expiresAt', 0)
        )
      except (json.JSONDecodeError, OSError):
        pass
    if keychain_expiry > file_expiry:
      host_creds.write_text(json.dumps(keychain_creds))
      host_creds.chmod(0o600)
  if host_creds.is_file():
    argv += ['-v', f'{host_creds}:/home/cw/.claude/.credentials.json']
  return [*argv, tag, 'claude', *claude_args]


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


def list_workspaces() -> int:
  proj = _project_root()
  worktrees_dir = proj / '.claude' / 'worktrees'
  containers_dir = proj / 'var' / 'cw' / 'containers'

  entries: list[tuple[str, bool, str, str | None, float | None]] = []
  if worktrees_dir.is_dir():
    for p in worktrees_dir.iterdir():
      if p.is_dir():
        kind = 'L' if _is_local_active(p.name) else 'X'
        pdir = _projects_dir_for_local(p.name, proj)
        entries.append((kind, False, p.name, _read_subject(pdir), _last_active(p)))
  if containers_dir.is_dir():
    mounts = _running_container_mounts()
    for p in containers_dir.iterdir():
      if p.is_dir():
        kind = 'C' if str(p) in mounts else 'X'
        pdir = _projects_dir_for_container(p.name)
        entries.append((kind, True, p.name, _read_subject(pdir), _last_active(p)))

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

  removed = 0
  skipped = 0

  if worktrees_dir.is_dir():
    for p in sorted(worktrees_dir.iterdir()):
      if not p.is_dir():
        continue
      if filter_refs is not None and p.name not in filter_refs:
        continue
      if _is_local_active(p.name):
        log.info('skip %s: active session', p.name)
        skipped += 1
        continue
      safe, reasons = _worktree_is_clean(p)
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

  if containers_dir.is_dir():
    mounts = _running_container_mounts()
    for p in sorted(containers_dir.iterdir()):
      if not p.is_dir():
        continue
      ref = _format_ref(p.name, True)
      if filter_refs is not None and ref not in filter_refs:
        continue
      if str(p) in mounts:
        log.info('skip %s: active session', ref)
        skipped += 1
        continue
      safe, reasons = _worktree_is_clean(p, container_proj=proj)
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


def cw(name: str, container: bool, drop: bool, claude_args: list[str]) -> int:
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    log.info('already inside a container; falling back to host mode')
    container = False

  if container:
    proj = _project_root()
    session = proj / 'var' / 'cw' / 'containers' / name
    session.mkdir(parents=True, exist_ok=True)
    tag = _image_tag()
    _ensure_image(tag)
    result = subprocess.run(_docker_run_argv(tag, name, proj, session, claude_args))
    if drop:
      shutil.rmtree(session)
      session_dir = Path.home() / '.claude' / 'cw-sessions' / name
      if session_dir.is_dir():
        shutil.rmtree(session_dir)
      log.info('removed container workspace %s', name)
    return result.returncode

  proj = _project_root()
  os.chdir(proj)

  if not drop:
    os.execvp('claude', ['claude', '-w', name, *claude_args])

  env = {**os.environ, 'CW_DROP': '1'}
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
  ss.add_argument(
    '--auto',
    action='store_true',
    help='let claude run autonomously, skipping most permissions (allowed only with -c)',
  )
  ss.add_argument(
    '--mcp', action='store_true', help='enable the local flow MCP server in the claude session'
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
  assert cmd == 'ss'
  auto = args.pop('auto')
  mcp = args.pop('mcp')
  if auto:
    if not args['container']:
      parser.error('--auto requires --container')
    args['claude_args'] = ['--dangerously-skip-permissions', *args['claude_args']]
  if mcp:
    args['claude_args'] = ['--mcp-config=flow/mcp/mcp.json', *args['claude_args']]
  args['claude_args'] = ['--remote-control', args['name'], *args['claude_args']]
  return cw(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
