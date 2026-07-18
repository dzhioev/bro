"""every executable shell script runs fail-fast: the `-e` shebang plus the
shared prelude (setup/prelude.sh — leveled logging and the command-not-found
guard). sourceable libraries carry no executable bit and stay out of scope."""

import re
import subprocess

from base.project_root import PROJECT_ROOT

SHEBANG = '#!/usr/bin/env -S bash -e'
PRELUDE_SOURCE = re.compile(r'^source .*/prelude\.sh"?$', re.MULTILINE)

# copied standalone into images, with no checkout around it to source the prelude from
EXEMPT = {'infra/server_base/install_launcher.sh'}


def executable_scripts() -> list[str]:
  listing = subprocess.run(
    ['git', 'ls-files', '-s', '*.sh'],
    capture_output=True,
    text=True,
    check=True,
    cwd=PROJECT_ROOT,
  ).stdout
  return [line.split('\t')[1] for line in listing.splitlines() if line.startswith('100755')]


def test_executable_scripts_found():
  assert len(executable_scripts()) > 30


def test_shebang_and_prelude():
  problems = []
  for path in executable_scripts():
    content = (PROJECT_ROOT / path).read_text()
    first_line = content.split('\n', 1)[0]
    if first_line != SHEBANG:
      problems.append(f'{path}: shebang is {first_line!r}, expected {SHEBANG!r}')
    if path not in EXEMPT and PRELUDE_SOURCE.search(content) is None:
      problems.append(f'{path}: does not source setup/prelude.sh')
  assert problems == []
