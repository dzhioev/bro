"""live-LLM probe of the spell interpreter behind `bro::cast`: a command whose
action no roster spell is named after or lists as a trigger resolves to an
error, however well the objects it mentions fit another spell's arguments,
while a listed trigger still resolves to its spell with the argument extracted
— without the words that selected it or the words that frame it, however long
or command-like the rest.
"""

import asyncio
import collections

import pytest

from bro.base.suite_environment import host_credential_store
from bro.registry import create_bro
from bro.spells import build_cast_tool, interpret, load_spell
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
    yield build_cast_tool(package.bro_class()(), harness='bro', hold='unattended')


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


# each command resolves the same way every time it is asked; a flaky
# interpretation shows up as a minority outcome across the attempts
_ATTEMPTS = 10
_LONG_PATH = (
  '/var/ride/session/claude/tmp/claude-1001/-workspace/a6116b5c-203a-4a78-bde0-b05e69828be9'
  '/scratchpad/seed/sol-2/test_a_session_waiting_on_a_wa0/tree/emit-line'
)
_PIPELINE = "tail -n +1 -f /var/log/app/server.log | grep --line-buffered -E 'ERROR|WARN'"
_TRAIL = '01m4a9nghq-h8f4dqje-bgapr756'


def _core_spells():
  return [load_spell(name, path) for name, path in create_bro('bro').spell_paths.items()]


async def _resolutions(command: str) -> list:
  with host_credential_store():
    return await asyncio.gather(*(interpret(command, _core_spells()) for _ in range(_ATTEMPTS)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ('command', 'expected'),
  [
    pytest.param(f'watch {_LONG_PATH}', ('watch', {'command': _LONG_PATH}), id='long-path'),
    pytest.param(
      'watch "/home/john doe/My Projects/emit line.sh" --verbose',
      ('watch', {'command': '"/home/john doe/My Projects/emit line.sh" --verbose'}),
      id='quoted-path',
    ),
    pytest.param(f'watch {_PIPELINE}', ('watch', {'command': _PIPELINE}), id='pipeline'),
    pytest.param(
      "watch watch -n 5 'kubectl get pods -n ride'",
      ('watch', {'command': "watch -n 5 'kubectl get pods -n ride'"}),
      id='command-named-like-the-spell',
    ),
    pytest.param(
      'keep watching the output of journalctl -fu ride-broker.service until the run ends',
      ('watch', {'command': 'journalctl -fu ride-broker.service'}),
      id='trigger-phrase',
    ),
    pytest.param('watch', None, id='missing-command'),
    pytest.param(
      'ask bro-eyebro to watch poll-pr dzhioev/bro 902 and report what it sees',
      ('ask', {}),
      id='spell-word-in-another-spells-request',
    ),
  ],
)
async def test_a_command_resolves_without_the_words_that_selected_its_spell(command, expected):
  resolved = await _resolutions(command)

  outcomes = collections.Counter(
    repr(None if isinstance(result, str) else (result[0].name, result[1])) for result in resolved
  )
  assert outcomes == {repr(expected): _ATTEMPTS}


@pytest.mark.asyncio
async def test_free_text_material_keeps_what_it_names_without_the_spell_word():
  resolved = await _resolutions(
    f'reflect on trail {_TRAIL} — focus on how it waited on its eyebro summon'
  )

  materials = [
    None if isinstance(result, str) else result[1].get('material', '') for result in resolved
  ]
  assert all(
    material is not None and _TRAIL in material and not material.lower().startswith('reflect')
    for material in materials
  ), materials
