"""live-LLM probe of the dev commit flow: a real `Dev` run commits a staged
change in a workspace carrying the persona's declared provisioning, and the
commit carries the token-accounting footer end to end — the agent commits
without bypassing or hand-writing anything, and the commit-msg hook appends the
footer from the run's own published usage.

`*_llm_test.py` semantics (roster exclusion, the BRO_LLM_TESTS=1 opt-in) are
owned by the root conftest.
"""

import subprocess
from typing import ClassVar

import pytest

import bro.llm.llms.openai as llm_llms_openai
import bro.llm.usage as usage
from bro.base import credentials
from bro.native.runner import Runner
from bros.dev import Dev

pytestmark = pytest.mark.skipif(
  not credentials.available('openai'), reason='needs the openai credential'
)


class _ProbeDev(Dev):
  # the tracker is irrelevant to the commit chain and its backend would bind to
  # the environment's ambient brog config; declared off, the probe runs the
  # same Dev everywhere
  features: ClassVar = {'brog': False}


def _git(repo, *args: str) -> str:
  return subprocess.run(
    ['git', '-C', str(repo), *args], check=True, capture_output=True, text=True
  ).stdout


@pytest.fixture
def staged_repo(tmp_path, monkeypatch):
  subprocess.run(['git', 'init', '-q', '-b', 'master', str(tmp_path)], check=True)
  _git(tmp_path, 'config', 'user.email', 'llm-test@example.com')
  _git(tmp_path, 'config', 'user.name', 'llm test')
  (tmp_path / '.gitignore').write_text('.token_accounting_state.json\n')
  (tmp_path / 'README.md').write_text('# sample project\n')
  _git(tmp_path, 'add', '.gitignore', 'README.md')
  _git(tmp_path, 'commit', '-qm', 'add the project readme')
  (tmp_path / 'notes.txt').write_text('a staged change for the probe\n')
  _git(tmp_path, 'add', 'notes.txt')
  # what ride's inner session layer applies before a real run starts
  _ProbeDev().provision_workspace(tmp_path)
  monkeypatch.chdir(tmp_path)
  return tmp_path


@pytest.mark.asyncio
async def test_dev_commit_carries_the_footer(staged_repo):
  runner = Runner(_ProbeDev.create(llm_llms_openai.LLMSpec(reasoning_effort='low')))
  await runner.run(
    'Commit the staged change in this repository with an appropriate message '
    'following the repository conventions. Do not push.',
    surface='llm-test',
    request_timeout=600,
  )
  assert 'notes.txt' not in _git(staged_repo, 'status', '--porcelain')
  message = _git(staged_repo, 'log', '-1', '--format=%B')
  footer = usage.parse_footer(message)
  assert footer is not None, f'no parseable footer in:\n{message}'
  assert footer.agents == ['bro//dev']
  assert message.count('> created with') == 1
