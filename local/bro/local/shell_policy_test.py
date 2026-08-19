from pathlib import Path

from bro.dev.shell_policy import assert_shell_policy

# packaged data rather than repository scripts: they are installed into an
# arbitrary repo's .git/hooks and may assume nothing beyond the venv on PATH
_PACKAGED_HOOKS = ('dev/bro/workflow/hooks/commit-msg', 'dev/bro/workflow/hooks/post-commit')


def test_repository_shell_policy():
  assert_shell_policy(Path(__file__).resolve().parents[3], exemptions=_PACKAGED_HOOKS)
