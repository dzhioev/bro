from typing import get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.spells import load_spell
from bro.summon import MAY_SUMMON_ENV
from bros.lead import Lead


def test_lead_declares_no_shell():
  for harness in get_args(mcp.Harness):
    selection = Lead()._selected_tools_for(harness)
    assert selection.shell_unrestricted is False
    assert selection.shell_commands == ()


def test_coordination_spells_render_for_every_surface():
  for path in Lead().spell_paths.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for granted in (('eyebro',), ()):
        mcp.render_text(
          spell.body,
          harness=harness,
          creds=spell_store.credentials.known_names(),
          may_summon=granted,
        )


def test_orchestrate_grants_a_derived_eyebro_to_every_pull_request_phase(monkeypatch):
  bro = Lead()
  ungranted = bro.get_spell_body('orchestrate', harness='claude')
  assert '@<the eyebro>' not in ungranted

  monkeypatch.setenv(MAY_SUMMON_ENV, 'bro-eyebro')
  granted = bro.get_spell_body('orchestrate', harness='claude')

  assert granted.count('`grant` `@<the eyebro>`') == 3
  assert granted.count('`grant`') == 4
