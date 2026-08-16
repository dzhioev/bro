from pathlib import Path

from bro.dev.packaging_policy import assert_packaging_policy


def test_repository_packaging_policy():
  assert_packaging_policy(Path(__file__).resolve().parents[3])
