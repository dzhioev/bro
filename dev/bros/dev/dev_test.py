from typing import ClassVar, get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.base.condition import SetVariable
from bro.dev import references
from bro.spells import load_spell
from bro.summon import MAY_SUMMON_ENV
from bros.dev import Dev


class _TrackerDev(Dev):
  name = 'tracker-dev'
  features: ClassVar = {'brog': True}


def test_style_reference_ships_with_the_dev_domain():
  assert references.dev_style.read().startswith('# Development style\n')


def test_claude_surface_selects_tracker_and_reference_tools(monkeypatch):
  monkeypatch.setattr(
    'bro.base.credentials.get_json',
    lambda name: {'backend': 'github', 'token': 't', 'repo': 'owner/repository'},
  )
  monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
  assert [
    server.namespace for server in Dev().assemble(harness='claude', wire='mcp', include_raise=False)
  ] == [
    'dev-style-source',
    'bro',
    'spell',
  ]

  monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'brog')
  assert [
    server.namespace for server in Dev().assemble(harness='claude', wire='mcp', include_raise=False)
  ] == [
    'brog',
    'dev-style-source',
    'bro',
    'spell',
  ]


def test_tracker_dev_inherits_shared_and_dev_spells():
  bro = _TrackerDev()
  # one spell per contributing package proves the MRO merge: reflect ships with
  # the shared bros/bro layer, fix with bros/dev
  assert 'reflect' in bro.spells
  assert 'fix' in bro.spells
  assert '## Spells' in bro.system_prompt
  assert '## Available skills' not in bro.system_prompt


def test_development_spells_render_for_every_surface():
  feature_names = frozenset({'brog'})
  for path in _TrackerDev().spells.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for wire in get_args(mcp.Wire):
        for enabled in (True, False):
          for granted in (('eyebro',), ()):
            mcp.render_text(
              spell.body,
              harness=harness,
              wire=wire,
              creds=spell_store.credentials.known_names(),
              may_summon=granted,
              extra={
                'features': SetVariable(
                  lambda name, on=enabled: on,
                  universe=feature_names,
                )
              },
            )


def test_run_pr_hands_review_to_a_granted_eyebro(monkeypatch):
  bro = _TrackerDev()
  solo = bro.get_spell_body('run-pr', harness='claude', wire='mcp')
  assert 'Hand the review to the eyebro' not in solo
  assert 'Pre-review by the eyebro' not in solo
  assert 'State the verdict as visible output' in solo
  monkeypatch.setenv(MAY_SUMMON_ENV, 'eyebro')
  granted = bro.get_spell_body('run-pr', harness='claude', wire='mcp')
  assert 'Hand the review to the eyebro' in granted
  assert 'Pre-review by the eyebro' in granted
  assert 'State the verdict as visible output' not in granted
