from typing import get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.spells import load_spell
from bro.summon import MAY_SUMMON_ENV
from bros.lead import Lead


def test_coordination_spells_render_for_every_surface():
  for path in Lead().spells.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for wire in get_args(mcp.Wire):
        for granted in (('eyebro',), ()):
          mcp.render_text(
            spell.body,
            harness=harness,
            wire=wire,
            creds=spell_store.credentials.known_names(),
            may_summon=granted,
          )


def test_orchestrate_grants_a_derived_eyebro_to_both_pull_request_phases(monkeypatch):
  bro = Lead()
  ungranted = bro.get_spell_body('orchestrate', harness='claude', wire='mcp')
  assert '@<the eyebro>' not in ungranted

  monkeypatch.setenv(MAY_SUMMON_ENV, 'bro-eyebro')
  granted = bro.get_spell_body('orchestrate', harness='claude', wire='mcp')

  assert granted.count('`grant` `@<the eyebro>`') == 2
