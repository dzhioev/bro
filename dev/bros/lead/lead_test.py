import pytest

from bro import registry
from bro.authority_view_test_helper import publish
from bro.base.text_window import BYTE_LIMIT
from bro.mcp import HOLDS
from bro.spells import call_text, load_spell
from bros.dev import Dev
from bros.eyebro import Eyebro
from bros.lead import Lead


class _StageReviewer(Eyebro):
  name = 'stage-reviewer'


class _StageLander(Dev):
  name = 'stage-lander'


@pytest.mark.parametrize('harness', ['bro', 'claude'])
@pytest.mark.parametrize('hold', HOLDS)
@pytest.mark.parametrize(
  ('targets', 'reviewer', 'lander'),
  [
    ((), False, False),
    (('eyebro',), True, False),
    (('dev',), False, True),
    (('eyebro', 'dev'), True, True),
    (('stage-reviewer', 'stage-lander'), True, True),
  ],
)
def test_orchestrate_selects_stage_delivery_and_coordinator_roles_within_one_window(
  monkeypatch, harness, hold, targets, reviewer, lander
):
  for persona in (_StageReviewer, _StageLander):
    monkeypatch.setitem(registry._REGISTRY, persona.name, persona)
  publish(monkeypatch, targets=targets)
  bro = Lead()
  body = bro.get_spell_body('orchestrate', harness=harness, hold=hold)

  assert 'with gates verdict and checks' in body
  assert ('**Reviewer coordinator.**' in body) == reviewer
  assert ('**Lander coordinator.**' in body) == lander
  assert '**Summoner approval.**' in body
  assert ('`grant` `@<the eyebro>`' in body) == reviewer

  spell = load_spell('orchestrate', bro.spell_paths['orchestrate'])
  arguments = {parameter.name: 'x' * 200 for parameter in spell.parameters}
  text = 'spell: spell::orchestrate\n\n' + call_text(spell, body, arguments)
  assert len(text) <= BYTE_LIMIT


def test_orchestrate_opens_the_root_task_through_the_shared_spell():
  bro = Lead()
  assert 'open-task' in bro.spell_paths
  assert '`brog::create_task`' in bro.get_spell_body(
    'open-task', harness='claude', hold='unattended'
  )
