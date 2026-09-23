"""the Claude Code a live probe drives, and the credential it signs in with.

A probe holds undocumented Claude Code behavior at the version managed sessions
run, so it drives that pinned release rather than whichever `claude` the host has
installed; the binary is downloaded once into pytest's cache."""

from pathlib import Path

import pytest

from bro.base import credentials
from bro.base.suite_environment import host_credential_store
from ride.claude import claude_release
from ride.workspace.build_context import claude_code_version


def claude_token() -> str | None:
  with host_credential_store():
    return credentials.try_get('claude_code')


REQUIRES_CLAUDE_CREDENTIAL = pytest.mark.skipif(
  claude_token() is None, reason='needs the claude_code credential'
)


def pinned_claude(config: pytest.Config) -> Path:
  """the pinned Claude Code binary for this host, fetched into the run's cache."""
  if config.cache is None:
    raise RuntimeError('the pinned claude is cached through pytest, whose cacheprovider is off')
  return claude_release.cached_binary(
    claude_code_version(), claude_release.host_platform(), config.cache.mkdir('claude-code')
  )
