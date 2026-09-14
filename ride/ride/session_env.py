"""the `--env` contract: an addition to a session's environment, as the CLI
takes it and as a record carries it."""

import re
from collections.abc import Mapping
from typing import Optional

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def env_assignments(values: Optional[list[str]]) -> dict[str, str]:
  """The `--env NAME=VALUE` additions as a mapping, in the order given."""
  additions: dict[str, str] = {}
  for value in values if values is not None else []:
    name, separator, assigned = value.partition('=')
    if separator == '' or _NAME.fullmatch(name) is None:
      raise ValueError(f'--env takes NAME=VALUE with an environment variable name, got {value!r}')
    if name in additions:
      raise ValueError(f'--env names {name} twice')
    additions[name] = assigned
  return additions


def env_additions(value: object) -> dict[str, str]:
  """`value` as a recorded mapping of additions, refused unless every name is
  an environment variable name and every value a string."""
  if not isinstance(value, Mapping):
    raise ValueError('env additions must be a mapping of names to string values')
  for name, assigned in value.items():
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
      raise ValueError(f'env addition {name!r} is not an environment variable name')
    if not isinstance(assigned, str):
      raise ValueError(f'env addition {name} must be a string value')
  return dict(value)
