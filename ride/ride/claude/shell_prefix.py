"""the Bash tool's shell, pinned, and the prefix that hands every command the session's PATH.

Claude Code runs a Bash command in a login shell whenever its startup shell
snapshot is missing, and a login profile that resets PATH there — Debian's
`/etc/profile`, or the user's own — drops the session's commands. Claude Code runs every command, hooks included, as
`<prefix> '<command string>'`; this prefix restores the session's PATH, drops
the two variables so nothing the command starts inherits them, and executes
the command string in the pinned shell.
"""

import os
import shlex
import shutil
from pathlib import Path

SHELL_ENV = 'CLAUDE_CODE_SHELL'
SHELL_PREFIX_ENV = 'CLAUDE_CODE_SHELL_PREFIX'
PREFIX_FILENAME = 'shell-prefix'
# Claude Code splits a prefix at its last " -" into a program and raw flags
_FLAG_SEPARATOR = ' -'


def session_shell(env: dict[str, str]) -> str:
  """the bash or zsh the session's commands run in: its own SHELL when that is
  one, else the bash on its PATH."""
  shell = env.get('SHELL')
  if shell is not None and Path(shell).name in ('bash', 'zsh') and os.access(shell, os.X_OK):
    return shell
  bash = shutil.which('bash', path=env['PATH'])
  if bash is None:
    raise RuntimeError("no bash on the session PATH to run claude's Bash commands in")
  return bash


def _prefix_script(shell: str, session_path: str) -> str:
  return (
    '#!/bin/sh\n'
    f'PATH={shlex.quote(session_path)}\n'
    'export PATH\n'
    f'unset {SHELL_ENV} {SHELL_PREFIX_ENV}\n'
    f'exec {shlex.quote(shell)} -c "$1"\n'
  )


def apply_shell_prefix(env: dict[str, str], directory: Path) -> None:
  """pin `env`'s shell for claude and route each of its Bash commands through a
  prefix, written into `directory`, that restores `env`'s PATH."""
  prefix = directory / PREFIX_FILENAME
  if _FLAG_SEPARATOR in str(prefix):
    raise RuntimeError(f'claude would split the shell prefix path {prefix} at "{_FLAG_SEPARATOR}"')
  shell = session_shell(env)
  prefix.write_text(_prefix_script(shell, env['PATH']))
  prefix.chmod(0o755)
  env[SHELL_ENV] = shell
  env[SHELL_PREFIX_ENV] = str(prefix)
