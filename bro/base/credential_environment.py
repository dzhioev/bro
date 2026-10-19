"""Environment overlays published by credential install hooks."""

import os
import shlex
from collections.abc import Mapping
from pathlib import Path

INSTALL_DIRECTORY_ENV = 'BRO_INSTALL_DIR'
ENVIRONMENT_FILE = 'environment.sh'


def render(values: Mapping[str, str | None]) -> str:
  return ''.join(
    f'unset {name}\n' if value is None else f'export {shlex.quote(name + "=" + value)}\n'
    for name, value in sorted(values.items())
  )


def overlay(environment: Mapping[str, str] | None = None) -> dict[str, str]:
  result = dict(os.environ if environment is None else environment)
  directory = result.get(INSTALL_DIRECTORY_ENV)
  if directory is None:
    return result
  tokens = shlex.split((Path(directory) / ENVIRONMENT_FILE).read_text())
  if len(tokens) % 2:
    raise ValueError('malformed credential environment file')
  for operation, assignment in zip(tokens[::2], tokens[1::2], strict=True):
    if operation == 'unset':
      result.pop(assignment, None)
    elif operation == 'export':
      name, value = assignment.split('=', 1)
      result[name] = value
    else:
      raise ValueError(f'invalid credential environment operation {operation!r}')
  return result
