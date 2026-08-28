from typing import get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.spells import load_spell
from bros.eyebro import Eyebro


def test_reviewer_carries_review_spells_but_no_author_procedures():
  bro = Eyebro()
  assert 'review-diff' in bro.spells
  assert 'review-pr' in bro.spells
  # reflect ships with the shared bros/bro layer, proving the MRO merge
  assert 'reflect' in bro.spells
  # a reviewer must not carry the author-side procedures; Eyebro derives from
  # Bro rather than Dev precisely to keep them out
  assert 'fix' not in bro.spells
  assert 'run-pr' not in bro.spells
  assert 'land' not in bro.spells


def test_claude_surface_selects_the_reference_tools():
  assert [
    server.namespace
    for server in Eyebro().assemble(harness='claude', wire='mcp', include_raise=False)
  ] == [
    'dev-style-source',
    'bro',
    'spell',
  ]


def test_review_spells_render_for_every_surface():
  for path in Eyebro().spells.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for wire in get_args(mcp.Wire):
        mcp.render_text(
          spell.body,
          harness=harness,
          wire=wire,
          creds=spell_store.credentials.known_names(),
        )
