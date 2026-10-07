from pathlib import Path
from typing import Optional

from bro.base.args import Parser

__cli_name__ = 'bro-shell-dir'

SHELL_DIR = Path(__file__).resolve().parent / 'setup'
_REQUIRED_FILES = (
  'prelude.sh',
  'install_awscli.sh',
  'log.sh',
  'strict.sh',
  'docker_smoke_test.sh',
  'uv-version',
)


def admit_command(command: str, *, commands: tuple[str, ...], unrestricted: bool) -> str:
  """Normalize a command and enforce a declaration's exact shell roster."""
  normalized = command.strip()
  if len(normalized) == 0:
    raise ValueError('command must be non-empty')
  if unrestricted or normalized in commands:
    return normalized
  listing = ', '.join(f'`{allowed}`' for allowed in commands)
  raise ValueError(
    f'this persona may run {listing} and nothing else — the command must match exactly, '
    'with nothing appended'
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
