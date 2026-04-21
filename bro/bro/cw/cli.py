#!/usr/bin/env python
"""create a git worktree and launch claude, optionally in an isolated docker container.

host mode (default): creates a git worktree under .claude/worktrees/<name>/ and
execs `claude -w <name>` from the project root.

container mode (--container): /workspace is a fresh clone, not a worktree — the
gitfile-based worktree layout doesn't survive the container boundary, and this
keeps the container's git state genuinely isolated. layout:

  - .claude/container-sessions/<name>/ on the host → /workspace rw
    (empty on first run; entrypoint clones host repo into it)
  - host project root → /host-repo ro
    (clone --shared reads objects from here via alternates; also the source for
    local submodule clones to avoid needing ssh keys in the container)
  - ~/.claude.json rw (auth tokens; refresh writes back to host)
  - ~/.claude → /host-claude ro (seeded once into the container-private
    ~/.claude/cw-sessions/<name>/, minus sessions/projects/history)

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


def _ensure_worktree(name: str) -> tuple[Path, Path]:
  proj = _project_root()
  worktree = proj / '.claude' / 'worktrees' / name
  branch = f'worktree-{name}'
  if worktree.is_dir():
    return proj, worktree

  has_branch = (
    subprocess.run(
      ['git', '-C', str(proj), 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}']
    ).returncode
    == 0
  )
  add_args = [str(worktree), branch] if has_branch else [str(worktree), '-b', branch]
  subprocess.run(['git', '-C', str(proj), 'worktree', 'add', *add_args], check=True)
  head = _git_out('-C', str(proj), 'rev-parse', 'HEAD')
  (proj / '.git' / 'worktrees' / name / 'CLAUDE_BASE').write_text(head + '\n')
  for key, val in (
    ('submodule.alternateLocation', 'superproject'),
    ('submodule.alternateErrorStrategy', 'info'),
  ):
    subprocess.run(['git', '-C', str(worktree), 'config', key, val], check=True)
  return proj, worktree


def _run_session_hook(worktree: Path) -> None:
  hook = worktree / '.claude' / 'hooks' / 'session_start.sh'
  if not (hook.is_file() and os.access(hook, os.X_OK)):
    return
  env = {**os.environ, 'CLAUDE_PROJECT_DIR': str(worktree)}
  subprocess.run([str(hook)], check=True, env=env)


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
  return [
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
    f'{home}/.gitconfig:/home/cw/.gitconfig:ro',
    '-e',
    'HOME=/home/cw',
    '-e',
    f'CW_NAME={name}',
    '-w',
    '/workspace',
    tag,
    'claude',
    '-w',
    name,
    *claude_args,
  ]


def cw(name: str, container: bool, claude_args: list[str]) -> int:
  if container and os.environ.get('CW_IN_CONTAINER'):
    logging.info('already inside a container; falling back to host mode')
    container = False

  if container:
    proj = _project_root()
    session = proj / '.claude' / 'container-sessions' / name
    session.mkdir(parents=True, exist_ok=True)
    tag = _image_tag()
    _ensure_image(tag)
    os.execvp('docker', _docker_run_argv(tag, name, proj, session, claude_args))

  proj, worktree = _ensure_worktree(name)
  _run_session_hook(worktree)
  os.chdir(proj)
  os.execvp('claude', ['claude', '-w', name, *claude_args])


def main(argv=None):
  parser = Parser(description='create a git worktree and launch claude')
  parser.add_argument(
    '-c', '--container', action='store_true', help='run claude inside an isolated docker container'
  )
  parser.add_argument('name', help='worktree name')
  parser.add_argument('claude_args', nargs=argparse.REMAINDER, help='args forwarded to claude')
  return cw(**parser.parse(argv))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
