from typing import ClassVar, get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.base.condition import SetVariable
from bro.dev import references
from bro.spells import load_spell
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
  assert set(bro.spells) == {'ask', 'audit', 'fix', 'land', 'reflect', 'run-pr', 'wire'}
  assert '## Spells' in bro.system_prompt
  assert '## Available skills' not in bro.system_prompt


def test_development_spells_render_for_every_surface():
  feature_names = frozenset({'brog'})
  for path in _TrackerDev().spells.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for wire in get_args(mcp.Wire):
        for enabled in (True, False):
          mcp.render_text(
            spell.body,
            harness=harness,
            wire=wire,
            creds=spell_store.credentials.known_names(),
            extra={
              'features': SetVariable(
                lambda name, on=enabled: on,
                universe=feature_names,
              )
            },
          )


def test_fix_declares_optional_task_and_new_arguments():
  bro = _TrackerDev()
  spell = load_spell('fix', bro.spells['fix'])
  assert [(parameter.name, parameter.required) for parameter in spell.parameters] == [
    ('task', False),
    ('new', False),
  ]

  for body in (
    bro.get_spell_body('fix', harness='bro', wire='bare'),
    bro.get_spell_body('fix', harness='claude', wire='mcp'),
  ):
    assert '`task` — operate on the existing task' in body
    assert '`new` — create a task from this seed' in body
    assert '/fix' not in body


def test_run_pr_declares_optional_base_and_reentry_arguments():
  bro = _TrackerDev()
  spell = load_spell('run-pr', bro.spells['run-pr'])
  assert [(parameter.name, parameter.required) for parameter in spell.parameters] == [
    ('base', False),
    ('pr', False),
  ]

  for body in (
    bro.get_spell_body('run-pr', harness='bro', wire='bare'),
    bro.get_spell_body('run-pr', harness='claude', wire='mcp'),
  ):
    assert '`base` — base the PR' in body
    assert '`pr` — re-entry mode' in body
    assert '/run-pr' not in body
