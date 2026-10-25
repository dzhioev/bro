from typing import ClassVar

import pytest

import bro.dev as dev
from bro.authority_view_test_helper import publish
from bro.dev import references
from bro.harness import get_harness
from bros.bro import Bro
from bros.dev import Dev


class _TrackerDev(Dev):
  name = 'tracker-dev'
  features: ClassVar = {'brog': True}


def test_style_reference_ships_with_the_dev_domain():
  assert references.dev_style.read().startswith('# Development style\n')


@pytest.mark.parametrize('guide', references.guides, ids=lambda guide: guide.name)
def test_each_guide_page_reads_as_a_titled_page(guide):
  assert guide.read().startswith('# ')


@pytest.mark.asyncio
async def test_manual_carries_every_development_guide():
  servers = Dev().assemble(harness='claude', hold='attended')
  manual = next(server for server in servers if server.namespace == 'man-source')
  [read] = await manual.list_tools()
  topics = read.parameters['properties']['topic']['enum']
  assert {guide.name for guide in references.guides} <= set(topics)


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
  publish(monkeypatch, targets=('eyebro',))
  assert 'eyebro' in bro.get_spell_body('run-pr', harness='claude', hold='unattended')
  assert 'eyebro' in bro.get_spell_body('land', harness='claude', hold='unattended')


def test_review_delegation_uses_one_asynchronous_tool_shape(monkeypatch):
  publish(monkeypatch, targets=('eyebro',))
  bro = _TrackerDev()

  native_body = bro.get_spell_body('run-pr', harness='bro', hold='unattended')
  claude_body = bro.get_spell_body('run-pr', harness='claude', hold='unattended')

  assert native_body == claude_body
  assert '`bro::summon` targeting the eyebro' in native_body
  assert 'detach' not in native_body


def test_the_task_spells_are_offered_only_where_a_tracker_resolves(monkeypatch):
  monkeypatch.setattr('bro.base.credentials.available', lambda name: False)
  assert {'fix', 'open-task'}.isdisjoint(Dev().spell_paths)
  assert 'run-pr' in Dev().spell_paths

  monkeypatch.setattr('bro.base.credentials.available', lambda name: name == 'brog')
  assert {'fix', 'open-task'} <= set(Dev().spell_paths)


def test_run_pr_cannot_be_offered_without_the_spells_it_runs():
  class Incomplete(Bro):
    name = 'incomplete'
    description = 'test bro'
    spells = (dev.spells.run_pr,)

  with pytest.raises(ValueError, match="requires spell 'land'"):
    Incomplete()


def test_run_pr_and_resume_pr_are_offered_together():
  class Pair(Bro):
    name = 'pair'
    description = 'test bro'
    spells = (dev.spells.run_pr, dev.spells.land, dev.spells.resume_pr)

  class RunPrOnly(Bro):
    name = 'run-pr-only'
    description = 'test bro'
    spells = (dev.spells.run_pr, dev.spells.land)

  assert {'run-pr', 'resume-pr'} <= set(Pair().spell_paths)
  with pytest.raises(ValueError, match="requires spell 'resume-pr'"):
    RunPrOnly()
