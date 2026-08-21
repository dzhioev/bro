from pathlib import Path

from bro.dev.markdown_policy import assert_markdown_policy


def test_repository_markdown_policy():
  assert_markdown_policy(Path(__file__).resolve().parents[3])
