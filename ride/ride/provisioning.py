"""Provision installed session harnesses on a host or in a relocatable runtime."""

import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

from bro.base.args import Parser
from bro.harness import installed_harness_names
from ride.errors import reports_runtime_errors
from ride.harness import get_harness


def relative_file(value: str) -> str:
  path = PurePosixPath(value)
  if (
    not path.parts
    or not value
    or path.is_absolute()
    or '..' in path.parts
    or path.as_posix() != value
  ):
    raise ValueError(f'invalid harness asset path {value!r}')
  return value


def bundle_file(value: str) -> str:
  relative_file(value)
  if PurePosixPath(value).parts[0] in ('venv', 'bin', 'bundle.json'):
    raise ValueError(f'harness asset uses a reserved runtime path: {value!r}')
  return value


def bundle_asset(root: Path, relative: str) -> Path:
  bundle_file(relative)
  path = root
  for part in PurePosixPath(relative).parts:
    path /= part
    if path.is_symlink():
      raise ValueError(f'harness assets must not use symbolic links: {path}')
  return path


def setup_harnesses() -> None:
  for name in installed_harness_names():
    get_harness(name).setup_runtime()


def provision_bundle(root: Path, target: tuple[str, ...]) -> dict[str, dict[str, str]]:
  contributions: dict[str, dict[str, str]] = {}
  claimed: set[str] = set()
  for name in installed_harness_names():
    files: dict[str, str] = {}
    for value in get_harness(name).provision_bundle(root, target):
      relative = bundle_file(value)
      if relative in claimed:
        raise ValueError(f'harness asset claimed twice: {relative}')
      claimed.add(relative)
      path = bundle_asset(root, relative)
      with path.open('rb') as file:
        files[relative] = hashlib.file_digest(file, 'sha256').hexdigest()
    contributions[name] = files
  return contributions


@reports_runtime_errors
def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='provision the installed harness runtimes')
  parser.add_argument('--bundle', type=Path, default=None, help='relocatable runtime root')
  parser.add_argument('--target', nargs='+', default=None, help='bundle target platform tuple')
  args = parser.parse(argv)
  if args['bundle'] is None:
    if args['target'] is not None:
      parser.error('--target requires --bundle')
    setup_harnesses()
  else:
    if args['target'] is None:
      parser.error('--bundle requires --target')
    print(json.dumps(provision_bundle(args['bundle'], tuple(args['target']))))
  return None


if __name__ == '__main__':
  sys.exit(main(sys.argv))
