from pathlib import Path

from bro_dev.shell_policy import assert_shell_policy


def test_framework_shell_policy():
  assert_shell_policy(Path(__file__).resolve().parent)
