import subprocess
from pathlib import Path
from typing import Optional

from bro.base.args import Parser
from bro.workflow.commit_footer import install_hooks
from bro.workspace.git import git_out


def install_repository(repository: Path) -> None:
  root = Path(git_out('rev-parse', '--show-toplevel', cwd=str(repository)))
  install_hooks(root)
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
