from pathlib import Path

from bro.dev.sleep_policy import assert_sleep_policy


def test_repository_sleep_policy():
  assert_sleep_policy(Path(__file__).resolve().parents[3])
