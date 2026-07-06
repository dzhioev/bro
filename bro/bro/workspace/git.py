import os
import secrets
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Optional


def git_out(*args: str, cwd: Optional[str] = None) -> str:
  return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


def git_run(
  *args: str, cwd: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> subprocess.CompletedProcess[str]:
  """run a git command, capturing stdout/stderr as text; returns the CompletedProcess.

  the common shape for git calls whose returncode (and sometimes stdout) is
  inspected rather than raising — wraps the repeated capture_output/text/env
  boilerplate so callers pass a cwd and an env overlay (e.g. no_prompt_env())."""
  return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=env)


def no_prompt_env() -> dict[str, str]:
  """os.environ overlaid with GIT_TERMINAL_PROMPT=0 so git fails fast on an
  unreachable remote instead of blocking on a credential prompt."""
  return {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}


def rev_parse_commit(root: Path, ref: str) -> Optional[str]:
  """resolve `ref` to a commit sha in the repo at `root`; None when it doesn't
  resolve there."""
  result = git_run('rev-parse', '--verify', f'{ref}^{{commit}}', cwd=root)
  return result.stdout.strip() if result.returncode == 0 else None


def _private_ref() -> str:
  """a uniquely named transfer ref: the nonce keeps concurrent resolutions in the
  same repo from reading each other's result."""
  return f'refs/cw/resolve-{secrets.token_hex(8)}'


def _claim_private_ref(root: Path, private: str) -> Optional[str]:
  """resolve a just-transferred private ref to its commit sha and delete the ref
  — the objects stay, callers hold the sha."""
  try:
    return rev_parse_commit(root, private)
  finally:
    git_run('update-ref', '-d', private, cwd=root)


def resolve_ref(root: Path, ref: str) -> Optional[str]:
  """resolve a branch/tag/sha to a commit sha in the repo at `root`, fetching it
  from origin when it isn't local — a feature branch pushed from a container has
  no local ref, so basing a workspace (or a summoned child) on it would otherwise
  fail. Returns None when neither the local lookup nor the origin fetch resolves."""
  local = rev_parse_commit(root, ref)
  if local is not None:
    return local
  private = _private_ref()
  fetch = git_run('fetch', 'origin', f'+{ref}:{private}', cwd=root, env=no_prompt_env())
  if fetch.returncode != 0:
    return None
  return _claim_private_ref(root, private)


def resolve_head(root: Path, repository: Path) -> Optional[str]:
  """the commit sha of `repository`'s current HEAD, with its objects present in
  the repo at `root` — the base-ref inheritance read.

  a repository sharing `root`'s object store (a worktree, a clone with no local
  commits) resolves directly; otherwise its HEAD is pushed from the repository
  into a private ref in `root` (deleted after the read — the objects stay).
  Returns None when the repository is missing or its HEAD cannot be read or
  transferred."""
  if not (repository / '.git').exists():
    return None
  # the overlay lets git read a clone whose alternates file names a path valid
  # only in its own mount namespace (a container clone's /host-repo). It is also
  # why the transfer is a push run at the repository, not a fetch run at root:
  # the history walk packs on the repository side, and git's local transport
  # strips repo-specific env from the remote half of a fetch, so only the pushing
  # process — our direct child — can carry the overlay to the walk.
  env = {
    **no_prompt_env(),
    'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(root / '.git' / 'objects'),
  }
  head = git_run('rev-parse', 'HEAD', cwd=repository, env=env)
  if head.returncode != 0:
    return None
  sha = head.stdout.strip()
  if rev_parse_commit(root, sha) is not None:
    return sha
  private = _private_ref()
  push = git_run('push', str(root), f'+HEAD:{private}', cwd=repository, env=env)
  if push.returncode != 0:
    return None
  return _claim_private_ref(root, private)
