from pathlib import Path

from bro_dev.shell_policy import assert_shell_policy

# packaged data rather than repository scripts: they are installed into an
# arbitrary repo's .git/hooks and may assume nothing beyond the venv on PATH
_PACKAGED_HOOKS = ('bro/workflow/hooks/commit-msg', 'bro/workflow/hooks/post-commit')


def test_repository_shell_policy():
  assert_shell_policy(Path(__file__).resolve().parent, exemptions=_PACKAGED_HOOKS)
