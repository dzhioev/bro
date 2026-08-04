import re
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path

SHEBANG = '#!/usr/bin/env -S bash -e'
PRELUDE_SOURCE = re.compile(r'^source .*/prelude\.sh"?$', re.MULTILINE)
_DEFAULT_EXEMPTIONS = frozenset({'setup.sh'})


def executable_scripts(repo_root: Path) -> list[str]:
  listing = subprocess.run(
    ['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
    capture_output=True,
    text=True,
    check=True,
    cwd=repo_root,
  ).stdout
  executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
  scripts = []
  for relative_path in listing.splitlines():
    path = repo_root / relative_path
    if not path.is_file() or path.stat().st_mode & executable_bits == 0:
      continue
    first_line = path.read_text().split('\n', 1)[0]
    if path.suffix == '.sh' or 'bash' in first_line:
      scripts.append(relative_path)
  return scripts


def assert_shell_policy(
  repo_root: Path,
  *,
  exemptions: Iterable[str] = (),
  excluded_directories: Iterable[str] = (),
) -> None:
  excluded = frozenset(excluded_directories)
  scripts = [
    path for path in executable_scripts(repo_root) if path.split('/', 1)[0] not in excluded
  ]
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
