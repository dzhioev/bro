import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

SHEBANG = '#!/usr/bin/env -S bash -e'
PRELUDE_SOURCE = re.compile(r'^source .*/prelude\.sh"?$', re.MULTILINE)
_DEFAULT_EXEMPTIONS = frozenset({'setup.sh'})


def executable_scripts(repo_root: Path) -> list[str]:
  listing = subprocess.run(
    ['git', 'ls-files', '-s', '*.sh'],
    capture_output=True,
    text=True,
    check=True,
    cwd=repo_root,
  ).stdout
  return [line.split('\t')[1] for line in listing.splitlines() if line.startswith('100755')]


def assert_shell_policy(repo_root: Path, *, exemptions: Iterable[str] = ()) -> None:
  scripts = executable_scripts(repo_root)
  if len(scripts) == 0:
    raise AssertionError(f'no executable shell scripts found under {repo_root}')

  exempt_paths = _DEFAULT_EXEMPTIONS | frozenset(exemptions)
  problems = []
  for relative_path in scripts:
    content = (repo_root / relative_path).read_text()
    first_line = content.split('\n', 1)[0]
    if first_line != SHEBANG:
      problems.append(f'{relative_path}: shebang is {first_line!r}, expected {SHEBANG!r}')
    if relative_path not in exempt_paths and PRELUDE_SOURCE.search(content) is None:
      problems.append(f'{relative_path}: does not source the shell prelude')
  if len(problems) > 0:
    raise AssertionError('\n'.join(problems))
