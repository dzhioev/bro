from pathlib import Path
from typing import Optional

from bro.base.args import Parser

__cli_name__ = 'bro-oops-dir'

ASSET_DIRECTORY = Path(__file__).resolve().parent / 'infra'
_REQUIRED_FILES = (
  'buildspec.yml',
  'deploy_lib.sh',
  'monitor_ecs.sh',
  'server_base/Dockerfile',
  'server_base/install_launcher.sh',
)


def asset_directory() -> Path:
  missing = [
    relative_path
    for relative_path in _REQUIRED_FILES
    if not (ASSET_DIRECTORY / relative_path).is_file()
  ]
  if len(missing) > 0:
    raise FileNotFoundError(
      f'missing packaged bro-oops files under {ASSET_DIRECTORY}: {", ".join(missing)}'
    )
  return ASSET_DIRECTORY


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='print the directory containing the bro-oops deployment assets')
  parser.parse(argv)
  print(asset_directory())
  return None
