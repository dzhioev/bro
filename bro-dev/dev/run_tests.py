#!/usr/bin/env python
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from bro.base.args import Parser

DIR = Path(__file__).resolve().parents[1]

DEPTRY_EXCLUDE = [
  '.*_test\\.py$',
  'conftest\\.py$',
  '.venv/',
  '.claude/',
]
DEPTRY_KNOWN_FIRST = ['bro', 'bro_dev']
PYTEST_FILES = [
  'bro_dev/sync_scripts_test.py',
  'bro_dev/bro_test.py',
  'bro_dev/claude_commit_footer_test.py',
  'bro_dev/git_golc_test.py',
  'bro_dev/usage_report_test.py',
  'bro_dev/install_test.py',
  'bro_dev/shell_policy_test.py',
]


def run(*args: str, extra_env: Optional[dict[str, str]] = None) -> None:
  env = None if extra_env is None else {**os.environ, **extra_env}
  subprocess.run(args, check=True, cwd=DIR, env=env)


def node_env() -> dict[str, str]:
  return {'NODE_OPTIONS': '--max-old-space-size=4096'}


def run_tests(argv: list[str]) -> Optional[int]:
  parser = Parser(description='run bro development-tool tests')
  parser.parse(argv)

  print('sync-scripts: verifying console-script metadata', file=sys.stderr)
  run(sys.executable, '-m', 'bro_dev.sync_scripts', '--check', '--project', str(DIR))
  print('ruff: lint check', file=sys.stderr)
  run(sys.executable, '-m', 'ruff', 'check', '.')

  deptry_args = [sys.executable, '-m', 'deptry', '.']
  for pattern in DEPTRY_EXCLUDE:
    deptry_args += ['-ee', pattern]
  for module in DEPTRY_KNOWN_FIRST:
    deptry_args += ['-kf', module]
  run(*deptry_args)

  print('pyright: type check', file=sys.stderr)
  run(sys.executable, '-m', 'pyright', extra_env=node_env())
  print('pytest: unit suite', file=sys.stderr)
  run(sys.executable, '-m', 'pytest', *PYTEST_FILES)
  return None


if __name__ == '__main__':
  sys.exit(run_tests(sys.argv))
