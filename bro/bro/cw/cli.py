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
import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

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
  logging.info('building %s', tag)
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
    f'{home}/.gitconfig:/host-gitconfig:ro',
    '-e',
    'HOME=/home/cw',
    '-e',
    f'CW_NAME={name}',
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


def list_workspaces() -> int:
  proj = _project_root()
  worktrees_dir = proj / '.claude' / 'worktrees'
  containers_dir = proj / 'var' / 'cw' / 'containers'

  entries: list[tuple[str, str]] = []
  if worktrees_dir.is_dir():
    for p in sorted(worktrees_dir.iterdir()):
      if p.is_dir():
        entries.append(('L' if _is_local_active(p.name) else 'X', p.name))
  if containers_dir.is_dir():
    mounts = _running_container_mounts()
    for p in sorted(containers_dir.iterdir()):
      if p.is_dir():
        entries.append(('C' if str(p) in mounts else 'X', p.name))

  for kind, name in entries:
    print(f'{kind}  {name}')
  return 0


def cw(name: str, container: bool, drop: bool, claude_args: list[str]) -> int:
  if container and os.environ.get('CW_IN_CONTAINER') is not None:
    logging.info('already inside a container; falling back to host mode')
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
  subprocess.run(['git', 'worktree', 'remove', '--force', str(worktree)], check=False)
  subprocess.run(['git', 'branch', '-D', f'worktree-{name}'], check=False)
  return result.returncode


def main(argv=None):
  parser = Parser(description='launch claude -w in the project root, or in a container')
  parser.add_argument(
    '-l', '--list', action='store_true', help='list workspaces (L=local, C=container, X=abandoned)'
  )
  parser.add_argument(
    '-c', '--container', action='store_true', help='run claude inside an isolated docker container'
  )
  parser.add_argument(
    '--drop', action='store_true', help='remove the worktree on exit without prompting (host mode)'
  )
  parser.add_argument('name', nargs='?', help='worktree name')
  parser.add_argument('claude_args', nargs=argparse.REMAINDER, help='args forwarded to claude')
  args = parser.parse(argv)
  if args.pop('list'):
    return list_workspaces()
  if args['name'] is None:
    parser.error('name is required (or pass --list)')
  return cw(**args)


if __name__ == '__main__':
  sys.exit(main(sys.argv))
