"""Live probe of the transcript directory the pinned Claude Code keeps for a
working directory, held against `ride.claude.claude_config.encode_project_path`:
a session's recorder, usage publisher, and `ride resume` look for its
transcripts there, and Claude documents no rule for the name it picks."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from ride.claude.claude_config import encode_project_path
from ride.claude.live_claude_test_helper import (
  REQUIRES_CLAUDE_CREDENTIAL,
  claude_token,
)

pytestmark = REQUIRES_CLAUDE_CREDENTIAL


@pytest.mark.parametrize(
  'directory',
  [
    pytest.param('under_score and space+plus@at.dot', id='punctuation'),
    pytest.param('café-\U0001d11e', id='non-ascii'),
    pytest.param(f'long-{"segment" * 30}', id='past-the-cut'),
  ],
)
def test_a_session_keeps_its_transcript_under_the_encoded_working_directory(
  tmp_path: Path, claude: Path, directory: str
) -> None:
  working = tmp_path / directory
  working.mkdir()
  config = tmp_path / 'config'
  config.mkdir()
  (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY')
  }
  env.update(
    CLAUDE_CODE_OAUTH_TOKEN=claude_token() or '',
    CLAUDE_CONFIG_DIR=str(config),
    DISABLE_AUTOUPDATER='1',
  )

  subprocess.run(
    [str(claude), '-p', '--model', 'haiku', 'Reply with OK and nothing else.'],
    cwd=working,
    env=env,
    check=True,
    capture_output=True,
    timeout=300,
  )

  projects = [path.name for path in (config / 'projects').iterdir()]
  assert projects == [encode_project_path(working)]
  assert any((config / 'projects' / projects[0]).glob('*.jsonl'))
