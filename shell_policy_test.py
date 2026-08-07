from pathlib import Path

from bro_dev.shell_policy import assert_shell_policy

# packaged data rather than a repository script: it is installed into an arbitrary
# consumer repo's .git/hooks and may assume nothing beyond the venv on PATH
_PACKAGED_HOOK = 'bro-dev/bro_dev/hooks/post-commit'


def test_repository_shell_policy():
  assert_shell_policy(Path(__file__).resolve().parent, exemptions=[_PACKAGED_HOOK])
