import os
from pathlib import Path
from unittest import mock

import pytest

from ride.claude import claude_release
from ride.workspace.build_context import claude_code_version


@pytest.fixture(scope='session')
def conformance_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """the runtime data root every conformance probe of a pytest run launches
  under, so the runtime bundle ride freezes there is paid once per run rather
  than once per probe."""
  path = tmp_path_factory.getbasetemp() / 'conformance-data'
  path.mkdir()
  return path


@pytest.fixture(scope='session')
def harness(request: pytest.FixtureRequest, conformance_data: Path) -> str:
  """the harness a conformance probe runs on, with its runtime ready in the run's
  data root: for Claude, this host's pinned Claude Code seeded from ride's
  host-wide release cache. Session-scoped, so that cache resolves before a test
  isolates the runtime data root (the root `conftest.py`)."""
  if request.param == 'claude':
    version = claude_code_version()
    platform_name = claude_release.host_platform()
    host_binary = claude_release.cached_binary(version, platform_name)
    with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(conformance_data)}):
      claude_release.seed_binary(host_binary, version, platform_name)
  return request.param
