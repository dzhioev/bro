from pathlib import Path

import pytest

from ride.claude import claude_release
from ride.workspace.build_context import claude_code_version


@pytest.fixture(scope='session')
def claude() -> Path:
  """the pinned Claude Code binary for this host, from ride's host-wide release
  cache; session-scoped, so it resolves before a test isolates the runtime data
  root (the root `conftest.py`) and is downloaded at most once per host."""
  return claude_release.cached_binary(claude_code_version(), claude_release.host_platform())
