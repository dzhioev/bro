from typing import ClassVar

from bro.dev import references
from bro.harness import get_harness
from bro.summon import LAUNCH_ENV, encode_launch
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

  def namespaces() -> set[str]:
    servers = Dev().assemble(harness='claude', hold='attended')
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
  assert '## Spells' in bro.composed_prompt('bro', hold='unattended')
  assert '## Available skills' not in bro.composed_prompt('bro', hold='unattended')


def test_gate_timeout_guidance_is_harness_neutral():
  bro = _TrackerDev()
  marker = 'explicit timeout large enough for the full run'

  for spell_name in ('run-pr', 'bump-bro'):
    native = bro.get_spell_body(spell_name, harness=get_harness('bro'), hold='unattended')
    claude = bro.get_spell_body(spell_name, harness=get_harness('claude'), hold='unattended')
    assert marker in native
    assert marker in claude


def test_review_delegation_renders_only_for_a_granted_eyebro(monkeypatch):
  bro = _TrackerDev()
  assert 'summon' not in bro.get_spell_body('run-pr', harness='claude', hold='unattended').lower()
  assert 'eyebro' not in bro.get_spell_body('land', harness='claude', hold='unattended')
  monkeypatch.setenv(
    LAUNCH_ENV,
    encode_launch({'bro': {'bros': frozenset({'eyebro'})}}),
  )
  assert 'eyebro' in bro.get_spell_body('run-pr', harness='claude', hold='unattended')
  assert 'eyebro' in bro.get_spell_body('land', harness='claude', hold='unattended')


def test_review_delegation_uses_one_asynchronous_tool_shape(monkeypatch):
  monkeypatch.setenv(
    LAUNCH_ENV,
    encode_launch({'bro': {'bros': frozenset({'eyebro'})}}),
  )
  bro = _TrackerDev()

  native_body = bro.get_spell_body('run-pr', harness='bro', hold='unattended')
  claude_body = bro.get_spell_body('run-pr', harness='claude', hold='unattended')

  assert native_body == claude_body
  assert '`bro::summon` targeting the eyebro' in native_body
  assert 'detach' not in native_body
