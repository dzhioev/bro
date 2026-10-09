"""A session's brash policy, and the argv a line of the session's shell reach starts as.

Under a finite command list a line runs in `brash` under the policy file the
session wrote from its reach; under `brash(ANY)` it runs in bash.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bro.base import spawn

if TYPE_CHECKING:
  from bro.mcp import Reach

# names a session's policy file to a process that starts its lines without having written it
POLICY_ENV = 'BRO_BRASH_POLICY'
POLICY_FILENAME = 'brash-policy.json'


def finite(reach: 'Reach') -> bool:
  """whether `reach` declares a finite command list, whose lines run in brash."""
  return reach.brash is not None and not reach.brash.unrestricted


def write(directory: Path, reach: 'Reach') -> Optional[Path]:
  """write the brash policy of `reach`'s finite command list into `directory` and
  return its path, or None where the reach declares no shell or an unrestricted
  one."""
  if not finite(reach):
    return None
  assert reach.brash is not None
  # brash parses as it imports; the bash route pays nothing for it
  from bro.brash import Policy

  path = directory / POLICY_FILENAME
  writable = reach.files is not None and reach.files.write
  Policy(entries=reach.brash.commands, writable=writable).write(path)
  return path


def published() -> Path:
  """the policy file the session's harness published in `POLICY_ENV`."""
  value = os.environ.get(POLICY_ENV)
  if value is None:
    raise RuntimeError(f'{POLICY_ENV} is unset: this session published no brash policy')
  return Path(value)


def line_argv(line: str, policy: Optional[Path]) -> list[str]:
  """the argv `line` starts as: brash under `policy`, or bash where there is none."""
  if policy is None:
    return ['bash', '-c', line]
  return [spawn.console_script('brash'), '--policy', str(policy), '-c', line]
