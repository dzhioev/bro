"""The home named by an installation's environment."""

import re
import shlex
import sys
from pathlib import Path

MARKER = 'bro-installation-home'
ENVIRONMENT = Path(sys.prefix)


def validate_home(value: object) -> str:
  if not isinstance(value, str) or re.fullmatch(r'[a-z][a-z0-9-]*', value) is None:
    raise ValueError(f'invalid home name {value!r}; expected [a-z][a-z0-9-]*')
  return value


def installation_home(environment: Path | None = None) -> str:
  marker = (ENVIRONMENT if environment is None else environment) / MARKER
  instruction = (
    f"write the installation's home with: printf '<home>\\n' > {shlex.quote(str(marker))}"
  )
  try:
    content = marker.read_bytes().decode('utf-8')
  except (OSError, UnicodeError) as error:
    raise ValueError(f'cannot read {marker}: {error}; {instruction}') from error
  if not content.endswith('\n'):
    raise ValueError(f'invalid marker {marker}: expected a home and a final newline; {instruction}')
  try:
    return validate_home(content[:-1])
  except ValueError as error:
    raise ValueError(f'invalid marker {marker}: {error}; {instruction}') from error
