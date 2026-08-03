from pathlib import Path
from typing import Optional

from bro.base.args import Parser

__cli_name__ = 'bro-shell-dir'

SHELL_DIR = Path(__file__).resolve().parent / 'setup'
_REQUIRED_FILES = (
  'prelude.sh',
  'log.sh',
  'strict.sh',
  'docker_smoke_test.sh',
  'base_image/build.sh',
  'base_image/Dockerfile',
)


def shell_dir() -> Path:
  missing = [
    relative_path for relative_path in _REQUIRED_FILES if not (SHELL_DIR / relative_path).is_file()
  ]
  if len(missing) > 0:
    raise FileNotFoundError(f'missing packaged shell files under {SHELL_DIR}: {", ".join(missing)}')
  return SHELL_DIR


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='print the directory containing the bro shell helpers')
  parser.parse(argv)
  print(shell_dir())
  return None
