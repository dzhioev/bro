import subprocess
from pathlib import Path

import pytest

from bro.dev.shell_policy import assert_shell_policy, executable_scripts, shell_files


def _write_executable(path: Path, content: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content)
  path.chmod(0o755)


def _git_repo(path: Path) -> None:
  subprocess.run(['git', 'init', '-q', str(path)], check=True)
  subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)


def test_shell_files_are_listed_by_suffix_or_shell_shebang(tmp_path):
  (tmp_path / 'library.sh').write_text('VALUE=1\n')
  _write_executable(tmp_path / 'hooks' / 'commit-msg', '#!/usr/bin/env -S bash -e\nexit 0\n')
  (tmp_path / 'launcher').write_text('#!/bin/sh\nexit 0\n')
  _write_executable(tmp_path / 'tool.py', '#!/usr/bin/env python\n')
  (tmp_path / 'notes.txt').write_text('sh\n')
  (tmp_path / 'blob.bin').write_bytes(b'\x89PNG\xff\xfe\n')
  _git_repo(tmp_path)

  assert shell_files(tmp_path) == ['hooks/commit-msg', 'launcher', 'library.sh']
  assert executable_scripts(tmp_path) == ['hooks/commit-msg']


def test_excluded_directories_are_outside_the_policy_surface(tmp_path):
  _write_executable(
    tmp_path / 'good.sh',
    '#!/usr/bin/env -S bash -e\nsource "$(bro-shell-dir)/prelude.sh"\n',
  )
  _write_executable(tmp_path / 'member' / 'bad.sh', '#!/bin/bash\n')
  _git_repo(tmp_path)

  assert_shell_policy(tmp_path, excluded_directories={'member'})


def test_reports_an_executable_without_the_required_prelude(tmp_path):
  _write_executable(tmp_path / 'bad.sh', '#!/usr/bin/env -S bash -e\n')
  _git_repo(tmp_path)

  with pytest.raises(AssertionError, match='does not source the shell prelude'):
    assert_shell_policy(tmp_path)
