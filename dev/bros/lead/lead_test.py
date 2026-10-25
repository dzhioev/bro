from bro.authority_view_test_helper import publish
from bros.lead import Lead


def test_orchestrate_grants_a_derived_eyebro_to_every_pull_request_phase(monkeypatch):
  bro = Lead()
  ungranted = bro.get_spell_body('orchestrate', harness='claude', hold='unattended')
  assert '@<the eyebro>' not in ungranted

  publish(monkeypatch, targets=('bro-eyebro',))
  granted = bro.get_spell_body('orchestrate', harness='claude', hold='unattended')

  assert granted.count('`grant` `@<the eyebro>`') == 3
  assert granted.count('`grant`') == 3


def test_orchestrate_opens_the_root_task_through_the_shared_spell():
  bro = Lead()
  assert 'open-task' in bro.spell_paths
  assert '`brog::create_task`' in bro.get_spell_body(
    'open-task', harness='claude', hold='unattended'
  )
