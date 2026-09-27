"""the gate stages a tree has passed, kept as git notes on the tree object."""

import os
import subprocess
from pathlib import Path
from typing import Optional

NOTES_REF = 'refs/notes/run-tests'

# a note is a commit on the notes ref, and a CI runner configures no identity
_IDENTITY = {
  'GIT_AUTHOR_NAME': 'run-tests',
  'GIT_AUTHOR_EMAIL': 'run-tests@localhost',
  'GIT_COMMITTER_NAME': 'run-tests',
  'GIT_COMMITTER_EMAIL': 'run-tests@localhost',
}


def _git(root: Path, *arguments: str, env: Optional[dict[str, str]] = None) -> str:
  return subprocess.run(
    ('git', *arguments), cwd=root, check=True, capture_output=True, text=True, env=env
  ).stdout


def clean_tree(root: Path) -> Optional[str]:
  """the tree HEAD names, when the worktree differs from it in nothing git does not ignore."""
  if _git(root, 'status', '--porcelain') != '':
    return None
  return _git(root, 'rev-parse', 'HEAD^{tree}').strip()


def passed(root: Path, tree: str) -> frozenset[tuple[str, str]]:
  """the (stage, work) pairs recorded as passing on `tree`."""
  for line in _git(root, 'notes', f'--ref={NOTES_REF}', 'list').splitlines():
    note, annotated = line.split()
    if annotated == tree:
      entries = _git(root, 'cat-file', 'blob', note).splitlines()
      return frozenset(_entry(entry) for entry in entries if entry != '')
  return frozenset()


def _entry(line: str) -> tuple[str, str]:
  stage, work = line.split(' ', 1)
  return stage, work


def record(root: Path, tree: str, stage: str, work: str) -> None:
  """note that `stage` passed over `work` on `tree`."""
  _git(
    root,
    'notes',
    f'--ref={NOTES_REF}',
    'append',
    '-m',
    f'{stage} {work}',
    tree,
    env={**os.environ, **_IDENTITY},
  )
