from bro.summon import LAUNCH_ENV, encode_launch
from bros.lead import Lead


def test_orchestrate_grants_a_derived_eyebro_to_every_pull_request_phase(monkeypatch):
  bro = Lead()
  ungranted = bro.get_spell_body('orchestrate', harness='claude', hold='unattended')
  assert '@<the eyebro>' not in ungranted

  monkeypatch.setenv(
    LAUNCH_ENV,
    encode_launch({'bro': {'bros': frozenset({'bro-eyebro'})}}),
  )
  granted = bro.get_spell_body('orchestrate', harness='claude', hold='unattended')

  assert granted.count('`grant` `@<the eyebro>`') == 3
  assert granted.count('`grant`') == 3
