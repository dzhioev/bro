import importlib.resources
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional

from bro.base.args import Parser


def _git_output(repository: Path, *args: str) -> str:
  return subprocess.run(
    ['git', *args],
    cwd=repository,
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()


def install_repository(repository: Path) -> None:
  root = Path(_git_output(repository, 'rev-parse', '--show-toplevel'))
  hooks_path = Path(_git_output(root, 'rev-parse', '--git-path', 'hooks'))
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
