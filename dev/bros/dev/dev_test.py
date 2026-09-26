from typing import ClassVar, get_args

import bro.mcp as mcp
from bro import spells as spell_store
from bro.base.condition import SetVariable
from bro.dev import references
from bro.spells import load_spell
from bro.summon import LAUNCH_ENV, encode_launch
from bros.dev import Dev


class _TrackerDev(Dev):
  name = 'tracker-dev'
  features: ClassVar = {'brog': True}


def test_style_reference_ships_with_the_dev_domain():
  assert references.dev_style.read().startswith('# Development style\n')


def test_dev_declares_an_unrestricted_shell_on_both_harnesses():
  for harness in get_args(mcp.Harness):
    assert Dev()._selected_tools_for(harness).shell_unrestricted is True


def test_claude_surface_selects_tracker_and_reference_tools(monkeypatch):
  monkeypatch.setattr(
    'bro.base.credentials.get_json',
    lambda name: {'backend': 'github', 'token': 't', 'repo': 'owner/repository'},
  )

  def namespaces() -> set[str]:
    servers = Dev().assemble(harness='claude', include_raise=False)
    return {server.namespace for server in servers}

  monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
  without_tracker = namespaces()
  assert 'dev' not in without_tracker
  assert 'dev-style-source' in without_tracker
  assert 'brog' not in without_tracker

  monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'brog')
  assert 'brog' in namespaces()


def test_tracker_dev_inherits_shared_and_dev_spells():
  bro = _TrackerDev()
  # one spell per contributing package proves the MRO merge: reflect ships with
  # the shared bros/bro layer, fix with bros/dev
  assert 'reflect' in bro.spell_paths
  assert 'fix' in bro.spell_paths
  assert '## Spells' in bro.system_prompt
  assert '## Available skills' not in bro.system_prompt


def test_development_spells_render_for_every_surface():
  feature_names = frozenset({'brog'})
  for path in _TrackerDev().spell_paths.values():
    spell = load_spell(path.stem, path)
    for harness in get_args(mcp.Harness):
      for enabled in (True, False):
        for granted in (('eyebro',), ()):
          mcp.render_text(
            spell.body,
            harness=harness,
            creds=spell_store.credentials.known_names(),
            may_summon=granted,
            extra={
              'features': SetVariable(
                lambda name, on=enabled: on,
                universe=feature_names,
              )
            },
          )


def test_review_delegation_renders_only_for_a_granted_eyebro(monkeypatch):
  bro = _TrackerDev()
  assert 'summon' not in bro.get_spell_body('run-pr', harness='claude').lower()
  assert 'eyebro' not in bro.get_spell_body('land', harness='claude')
  monkeypatch.setenv(
    LAUNCH_ENV,
    encode_launch({'bro': {'bros': frozenset({'eyebro'})}}),
  )
  assert 'eyebro' in bro.get_spell_body('run-pr', harness='claude')
  assert 'eyebro' in bro.get_spell_body('land', harness='claude')


def test_review_delegation_detaches_only_on_the_claude_harness(monkeypatch):
  monkeypatch.setenv(
    LAUNCH_ENV,
    encode_launch({'bro': {'bros': frozenset({'eyebro'})}}),
  )
  bro = _TrackerDev()

  native_body = bro.get_spell_body('run-pr', harness='bro')
  claude_body = bro.get_spell_body('run-pr', harness='claude')

  assert 'with `detach: true`' not in native_body
  assert 'with `detach: true`' in claude_body
