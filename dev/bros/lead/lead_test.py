import bro.mcp as mcp
from bro import spells as spell_store
from bro.harness import installed_harness_names
from bro.spells import load_spell
from bro.summon import LAUNCH_ENV, encode_launch
from bros.lead import Lead


def test_coordination_spells_render_for_every_surface():
  for path in Lead().spell_paths.values():
    spell = load_spell(path.stem, path)
    for harness in installed_harness_names():
      for hold in mcp.HOLDS:
        for granted in (('eyebro',), ()):
          mcp.render_text(
            spell.body,
            harness=harness,
            creds=spell_store.credentials.known_names(),
            may_summon=granted,
            hold=hold,
          )


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
