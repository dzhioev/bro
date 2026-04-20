#!/usr/bin/env python
"""create a git worktree and launch claude, optionally in an isolated docker container.

with --container, the worktree is bind-mounted at /workspace and claude runs
inside. ~/.claude.json is bind-mounted rw so auth tokens work (and refresh
writes back to host). ~/.claude/ is NOT shared: each worktree gets its own
host-side directory at ~/.claude/cw-sessions/<name>/, seeded on first run
from a read-only mount of the host's ~/.claude/ minus sensitive transcript
data (sessions/projects/history.jsonl/cw-sessions). network is not restricted
by design.
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


def _ensure_worktree(name: str) -> tuple[Path, Path]:
  common = Path(_git_out('rev-parse', '--git-common-dir')).resolve()
  proj = common.parent
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
  (common / 'worktrees' / name / 'CLAUDE_BASE').write_text(head + '\n')
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


def _docker_run_argv(tag: str, name: str, worktree: Path, claude_args: list[str]) -> list[str]:
  home = Path.home()
  claude_dir = home / '.claude' / 'cw-sessions' / name
  claude_dir.mkdir(parents=True, exist_ok=True)
  return [
    'docker',
    'run',
    '-it',
    '--rm',
    '-v',
    f'{worktree}:/workspace',
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

  proj, worktree = _ensure_worktree(name)
  _run_session_hook(worktree)

  if container:
    tag = _image_tag()
    _ensure_image(tag)
    os.execvp('docker', _docker_run_argv(tag, name, worktree, claude_args))

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
