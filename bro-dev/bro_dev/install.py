import importlib.resources
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional

from bro.base.args import Parser
from bro.workspace.git import git_out


def install_repository(repository: Path) -> None:
  root = Path(git_out('rev-parse', '--show-toplevel', cwd=str(repository)))
  hooks_path = Path(git_out('rev-parse', '--git-path', 'hooks', cwd=str(root)))
  if not hooks_path.is_absolute():
    hooks_path = root / hooks_path
  hooks_path.mkdir(parents=True, exist_ok=True)

  resource = importlib.resources.files('bro_dev').joinpath('hooks/post-commit')
  with importlib.resources.as_file(resource) as source:
    destination = hooks_path / 'post-commit'
    shutil.copyfile(source, destination)
  destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

  subprocess.run(
    ['git', 'config', '--local', 'alias.golc', '!bro-dev.git-golc'],
    cwd=root,
    check=True,
  )


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='install bro development hooks in the current repository')
  parser.parse(argv)
  install_repository(Path.cwd())
  return None
