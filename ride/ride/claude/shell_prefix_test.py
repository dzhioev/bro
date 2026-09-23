import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ride.claude.shell_prefix import (
  SHELL_ENV,
  SHELL_PREFIX_ENV,
  apply_shell_prefix,
  session_shell,
)

_BASH = shutil.which('bash')
_CLOBBERED_PATH = '/usr/local/bin:/usr/bin:/bin'


def _session_env(tmp_path: Path) -> dict[str, str]:
  session_bin = tmp_path / 'session bin'
  session_bin.mkdir()
  return {'PATH': f'{session_bin}:{os.environ["PATH"]}', 'SHELL': str(_BASH)}


def _run_through_prefix(env: dict[str, str], command: str) -> str:
  """run `command` the way claude runs it through the prefix, from a login
  shell whose profile reset PATH."""
  completed = subprocess.run(
    [env[SHELL_PREFIX_ENV], command],
    env={**env, 'PATH': _CLOBBERED_PATH},
    capture_output=True,
    text=True,
    check=True,
  )
  return completed.stdout


def test_a_command_runs_under_the_session_path_after_a_login_shell_reset_it(tmp_path):
  env = _session_env(tmp_path)
  session_path = env['PATH']
  apply_shell_prefix(env, tmp_path)

  assert _run_through_prefix(env, 'echo "$PATH"') == f'{session_path}\n'


def test_a_command_s_own_path_override_still_applies(tmp_path):
  env = _session_env(tmp_path)
  apply_shell_prefix(env, tmp_path)

  output = _run_through_prefix(env, 'PATH="/override:$PATH"; echo "$PATH"')

  assert output.startswith('/override:')


def test_nothing_the_command_starts_inherits_the_prefix_variables(tmp_path):
  env = _session_env(tmp_path)
  apply_shell_prefix(env, tmp_path)

  output = _run_through_prefix(
    env, f'echo "${{{SHELL_ENV}:-unset}} ${{{SHELL_PREFIX_ENV}:-unset}}"'
  )

  assert output == 'unset unset\n'


def test_claude_is_pinned_to_the_shell_the_prefix_executes(tmp_path):
  env = _session_env(tmp_path)
  apply_shell_prefix(env, tmp_path)

  assert env[SHELL_ENV] == _BASH
  assert _run_through_prefix(env, 'echo "$BASH"') == f'{_BASH}\n'


def test_a_shell_that_is_neither_bash_nor_zsh_gives_way_to_bash(tmp_path):
  env = {'PATH': os.environ['PATH'], 'SHELL': '/usr/bin/fish'}

  assert session_shell(env) == _BASH


def test_a_session_without_bash_is_refused(tmp_path):
  with pytest.raises(RuntimeError, match='no bash'):
    session_shell({'PATH': str(tmp_path), 'SHELL': '/usr/bin/fish'})


def test_a_prefix_path_claude_would_split_is_refused(tmp_path):
  directory = tmp_path / 'state -dir'
  directory.mkdir()

  with pytest.raises(RuntimeError, match='would split'):
    apply_shell_prefix(_session_env(tmp_path), directory)
