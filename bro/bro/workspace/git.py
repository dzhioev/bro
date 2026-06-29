import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Optional


def git_out(*args: str, cwd: Optional[str] = None) -> str:
  return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


def git_run(
  *args: str, cwd: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> subprocess.CompletedProcess[str]:
  """run a git command, capturing stdout/stderr as text; returns the CompletedProcess.

  the common shape for git calls whose returncode (and sometimes stdout) is
  inspected rather than raising — wraps the repeated capture_output/text/env
  boilerplate so callers pass a cwd and an env overlay (e.g. no_prompt_env())."""
  return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=env)


def no_prompt_env() -> dict[str, str]:
  """os.environ overlaid with GIT_TERMINAL_PROMPT=0 so git fails fast on an
  unreachable remote instead of blocking on a credential prompt."""
  return {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
