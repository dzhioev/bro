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


def _image_tag() -> str:
  h = hashlib.sha256()
  for path in sorted(CONTAINER_DIR.iterdir()):
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
  github_token = (proj / '.configs' / 'cw_github_token').resolve()
  if github_token.is_file():
    argv += ['-v', f'{github_token}:/run/secrets/github_token:ro']
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


def list_workspaces() -> int:
  proj = _project_root()
  worktrees_dir = proj / '.claude' / 'worktrees'
  containers_dir = proj / 'var' / 'cw' / 'containers'

  entries: list[tuple[str, str, str | None, float | None]] = []
  if worktrees_dir.is_dir():
    for p in worktrees_dir.iterdir():
      if p.is_dir():
        kind = 'L' if _is_local_active(p.name) else 'X'
        pdir = _projects_dir_for_local(p.name, proj)
        entries.append((kind, p.name, _read_subject(pdir), _last_active(p)))
  if containers_dir.is_dir():
    mounts = _running_container_mounts()
    for p in containers_dir.iterdir():
      if p.is_dir():
        kind = 'C' if str(p) in mounts else 'X'
        pdir = _projects_dir_for_container(p.name)
        entries.append((kind, p.name, _read_subject(pdir), _last_active(p)))

  if len(entries) == 0:
    return 0
  entries.sort(key=lambda e: (_KIND_ORDER[e[0]], e[1]))
  name_w = max(len(name) for _, name, _, _ in entries)
  ages = [_format_age(mtime) if mtime is not None else '' for _, _, _, mtime in entries]
  age_w = max(len(a) for a in ages) if len(ages) > 0 else 0
  for (kind, name, subject, _), age in zip(entries, ages):
    badge = _BADGES[kind]
    age_col = f'  {age:<{age_w}}' if len(age) > 0 else ' ' * (age_w + 2)
    if subject is None:
      print(f'{badge} {name:<{name_w}}{age_col}')
    else:
      print(f'{badge} {name:<{name_w}}{age_col}  {_truncate(subject, 80)}')
  return 0


def _worktree_is_clean(path: Path) -> tuple[bool, list[str]]:
  """check whether a worktree is safe to remove.

  returns (safe, reasons) where reasons lists what prevents removal.
  """
  no_prompt_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
  reasons: list[str] = []
  status = subprocess.run(
    ['git', 'status', '--porcelain'], cwd=path, capture_output=True, text=True, env=no_prompt_env
  )
  if status.returncode != 0:
    reasons.append('cannot read git status')
    return False, reasons
  if len(status.stdout.strip()) > 0:
    reasons.append('uncommitted or untracked changes')

  subprocess.run(
    ['git', 'fetch', '--quiet', 'origin', 'master'],
    cwd=path,
    capture_output=True,
    env=no_prompt_env,
  )
  master_check = subprocess.run(
    ['git', 'rev-parse', '--verify', 'origin/master'],
    cwd=path,
    capture_output=True,
  )
  if master_check.returncode != 0:
    reasons.append('origin/master not found')
  else:
    ancestor = subprocess.run(
      ['git', 'merge-base', '--is-ancestor', 'HEAD', 'origin/master'],
      cwd=path,
      capture_output=True,
    )
    if ancestor.returncode != 0:
      ahead = subprocess.run(
        ['git', 'rev-list', '--count', 'HEAD', '^origin/master'],
        cwd=path,
        capture_output=True,
        text=True,
      )
      n = ahead.stdout.strip() if ahead.returncode == 0 else '?'
      reasons.append(f'{n} commit(s) not on origin/master')

  return len(reasons) == 0, reasons


def clean_workspaces(force: bool = False) -> int:
  proj = _project_root()
  worktrees_dir = proj / '.claude' / 'worktrees'
  containers_dir = proj / 'var' / 'cw' / 'containers'

  removed = 0
  skipped = 0

  if worktrees_dir.is_dir():
    for p in sorted(worktrees_dir.iterdir()):
      if not p.is_dir():
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
      if str(p) in mounts:
        log.info('skip %s (container): active session', p.name)
        skipped += 1
        continue
      safe, reasons = _worktree_is_clean(p)
      if not safe:
        if not force:
          log.info('skip %s (container): %s', p.name, '; '.join(reasons))
          skipped += 1
          continue
        log.info('force %s (container): %s', p.name, '; '.join(reasons))
      shutil.rmtree(p)
      session_dir = Path.home() / '.claude' / 'cw-sessions' / p.name
      if session_dir.is_dir():
        shutil.rmtree(session_dir)
      log.info('removed %s (container)', p.name)
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
    os.execvp('docker', _docker_run_argv(tag, name, proj, session, claude_args))

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
  parser = Parser(description='launch claude -w in the project root, or in a container')
  parser.add_argument(
    '-l',
    '--list',
    action='store_true',
    help='list workspaces ([.]=local, [o]=container, [x]=abandoned)',
  )
  parser.add_argument(
    '--clean',
    action='store_true',
    help='remove stale workspaces that have no uncommitted or unpushed changes',
  )
  parser.add_argument(
    '--force',
    action='store_true',
    help='with --clean, remove workspaces even if they have uncommitted or unpushed changes',
  )
  parser.add_argument(
    '-c', '--container', action='store_true', help='run claude inside an isolated docker container'
  )
  parser.add_argument(
    '--drop', action='store_true', help='remove the worktree on exit without prompting (host mode)'
  )
  parser.add_argument(
    '--auto',
    action='store_true',
    help='let claude run autonomously, skipping most permissions (allowed only with -c)',
  )
  parser.add_argument('name', nargs='?', help='worktree name')
  parser.add_argument('claude_args', nargs=argparse.REMAINDER, help='args forwarded to claude')
  args = parser.parse(argv)
  if args.pop('list'):
    args.pop('force')
    return list_workspaces()
  force = args.pop('force')
  if args.pop('clean'):
    return clean_workspaces(force=force)
  if args['name'] is None:
    parser.error('name is required (or pass --list)')
  auto = args.pop('auto')
  if auto:
    if not args['container']:
      parser.error('--auto requires --container')
    args['claude_args'] = ['--dangerously-skip-permissions', *args['claude_args']]
  args['claude_args'] = ['--remote-control', *args['claude_args']]
  return cw(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
