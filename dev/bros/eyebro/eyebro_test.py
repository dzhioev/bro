from typing import get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.base.condition import SetVariable
from bro.spells import load_spell
from bros.eyebro import Eyebro


def test_reviewer_carries_review_spells_but_no_author_procedures():
  bro = Eyebro()
  assert 'review-diff' in bro.spell_paths
  assert 'review-pr' in bro.spell_paths
  # reflect ships with the shared bros/bro layer, proving the MRO merge
  assert 'reflect' in bro.spell_paths
  # a reviewer must not carry the author-side procedures; Eyebro derives from
  # Bro rather than Dev precisely to keep them out
  assert 'fix' not in bro.spell_paths
  assert 'run-pr' not in bro.spell_paths
  assert 'land' not in bro.spell_paths


def test_claude_surface_selects_the_reference_tools():
  assert [
    server.namespace
    for server in Eyebro().assemble(harness='claude', wire='mcp', include_raise=False)
  ] == [
    'dev-style-source',
    'bro',
    'spell',
  ]


def test_github_is_the_reviewers_best_effort_credential():
  bro = Eyebro()
  assert 'github' in bro.optional_secrets(harness='claude')
  assert 'github' not in bro.needed_secrets(harness='claude')


def test_review_spells_render_for_every_surface():
  feature_names = frozenset({'github'})
  for path in Eyebro().spell_paths.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for wire in get_args(mcp.Wire):
        for enabled in (True, False):
          mcp.render_text(
            spell.body,
            harness=harness,
            wire=wire,
            creds=spell_store.credentials.known_names(),
            extra={'features': SetVariable(lambda name, on=enabled: on, universe=feature_names)},
          )


def test_review_pr_drives_the_pull_request_only_under_a_github_identity(monkeypatch):
  bro = Eyebro()
  monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'github')
  with_identity = bro.get_spell_body('review-pr', harness='claude', wire='mcp')
  assert 'pr-state' in with_identity
  assert 'no GitHub identity' not in with_identity

  monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
  without_identity = bro.get_spell_body('review-pr', harness='claude', wire='mcp')
  assert 'no GitHub identity' in without_identity
  assert 'pr-state' not in without_identity
