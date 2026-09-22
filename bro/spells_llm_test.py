"""live-LLM probe of the spell interpreter behind `bro::cast`: a command whose
action no roster spell is named after or lists as a trigger resolves to an
error, however well the objects it mentions fit another spell's arguments,
while a listed trigger still resolves to its spell with the argument extracted.
"""

import pytest

from bro.base.suite_environment import host_credential_store
from bro.spells import build_cast_tool
from bro.spells_test_helper import spell_package

ISSUE = 'https://github.com/example/project/issues/637'

FIX = """\
---
description: use when the user points you at a task and asks you to work on it — "fix it", "work on this task", "let's do <url>"
parameters: {"task?": "ref of the existing task to work on"}
---

procedure body
"""

RUN_PR = """\
---
description: use when the worktree's changes are ready for review and a pull request should be opened — "PR it", "open a PR", "resume PR <url>"
parameters: {"pr?": "existing pull request URL or number to resume"}
---

procedure body
"""


@pytest.fixture
def cast(tmp_path, monkeypatch):
  package = spell_package(monkeypatch, tmp_path, '_cast_probe', {'fix': FIX, 'run-pr': RUN_PR})
  with host_credential_store():
    yield build_cast_tool(package.bro_class()(), harness='bro')


@pytest.mark.asyncio
async def test_an_action_no_spell_answers_to_is_an_error(cast):
  result = await cast.call({'command': f'orchestrate development of the {ISSUE} feature'})
  assert isinstance(result, dict), result
  assert 'orchestrate' in result['error']


@pytest.mark.asyncio
async def test_a_listed_trigger_without_arguments_resolves(cast):
  result = await cast.call({'command': 'PR it'})
  assert result == 'spell: spell::run-pr\n\nprocedure body', result


@pytest.mark.asyncio
async def test_a_listed_trigger_resolves_with_its_argument(cast):
  result = await cast.call({'command': f'work on {ISSUE}'})
  assert isinstance(result, str), result
  assert result.startswith('spell: spell::fix\n')
  assert f'task: {ISSUE}' in result
