import os
import subprocess
from pathlib import Path
from typing import Optional

from bro.base.args import Parser
from bro.workflow.commit_footer import install_hooks
from bro.workspace.git import git_out
from bro.workspace.paths import in_container, project_root, runtime_root


def _provision_runtime_root(repository: Path) -> None:
  root = runtime_root(repository)
  if root.is_dir() and root.stat().st_uid == os.getuid():
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
      raise RuntimeError(f'{root} is owned by uid {os.getuid()} but is not accessible')
    return
  if root.exists() and not root.is_dir():
    raise RuntimeError(f'{root} exists but is not a directory')
  subprocess.run(['sudo', 'mkdir', '-p', str(root)], check=True)
  subprocess.run(['sudo', 'chown', f'{os.getuid()}:{os.getgid()}', str(root)], check=True)
  subprocess.run(['sudo', 'chmod', '0700', str(root)], check=True)


def install_repository(repository: Path) -> None:
  root = Path(git_out('rev-parse', '--show-toplevel', cwd=str(repository)))
  if not in_container():
    _provision_runtime_root(project_root(root))
  install_hooks(root)
  subprocess.run(
    ['git', 'config', '--local', 'alias.golc', '!bro.dev.git-golc'],
    cwd=root,
    check=True,
  )


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='install bro development hooks and runtime state in this repository')
  parser.parse(argv)
  install_repository(Path.cwd())
  return None
