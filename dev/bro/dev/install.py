import subprocess
from pathlib import Path
from typing import Optional

from bro.base.args import Parser
from bro.workflow.commit_footer import install_hooks
from bro.workspace.git import git_out

BLAME_IGNORE_FILE = '.git-blame-ignore-revs'


def install_repository(repository: Path) -> None:
  root = Path(git_out('rev-parse', '--show-toplevel', cwd=str(repository)))
  install_hooks(root)
  subprocess.run(
    ['git', 'config', '--local', 'alias.golc', '!bro.dev.git-golc'],
    cwd=root,
    check=True,
  )
  # git fails every blame when the configured list is missing, so the setting
  # follows the file rather than being declared once and left behind
  if (root / BLAME_IGNORE_FILE).is_file():
    subprocess.run(
      ['git', 'config', '--local', 'blame.ignoreRevsFile', BLAME_IGNORE_FILE],
      cwd=root,
      check=True,
    )


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='install the bro development hooks in this repository')
  parser.parse(argv)
  install_repository(Path.cwd())
  return None
