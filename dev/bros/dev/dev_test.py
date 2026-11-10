from contextlib import closing
from typing import ClassVar

import pytest

import bro.dev as dev
from bro.authority_view_test_helper import publish
from bro.base.text_window import BYTE_LIMIT
from bro.datasources.references import llm_providers
from bro.dev import references
from bro.harness import get_harness
from bro.monitor import SESSION_DIR_ENV
from bros.bro import Bro
from bros.dev import Dev
from bros.eyebro import Eyebro
from bros.lead import Lead


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
  # one spell per contributing package proves the MRO merge: ask ships with
  # the shared bros/bro layer, fix with bros/dev
  assert 'ask' in bro.spell_paths
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
  marker = 'use `bro::summon` targeting the eyebro'
  assert marker not in bro.get_spell_body('run-pr', harness='claude', hold='unattended')
  publish(monkeypatch, targets=('eyebro',))
  assert marker in bro.get_spell_body('run-pr', harness='claude', hold='unattended')


@pytest.mark.parametrize('landing', ['merge', 'push'])
@pytest.mark.parametrize('harness', ['bro', 'claude'])
def test_land_selects_only_its_method_within_one_window(landing, harness):
  body = _TrackerDev().get_spell_body(
    'land', harness=harness, hold='unattended', choices={'landing': landing}
  )
  markers = {'merge': '## 3. Merge through `land-pr`', 'push': '## 3. Push through `land-push`'}
  assert markers[landing] in body
  assert markers['push' if landing == 'merge' else 'merge'] not in body
  assert len(body) <= BYTE_LIMIT


def test_land_without_arguments_selects_merge():
  bro = _TrackerDev()
  assert bro.get_spell_body('land', harness='bro', hold='unattended') == bro.get_spell_body(
    'land', harness='bro', hold='unattended', choices={'landing': 'merge'}
  )


def test_review_delegation_uses_one_asynchronous_tool_shape(monkeypatch):
  publish(monkeypatch, targets=('eyebro',))
  bro = _TrackerDev()

  native_body = bro.get_spell_body('run-pr', harness='bro', hold='unattended')
  claude_body = bro.get_spell_body('run-pr', harness='claude', hold='unattended')

  assert native_body == claude_body
  assert 'use `bro::summon` targeting the eyebro' in native_body
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


def test_run_pr_requires_its_follow_pr_handoff():
  class Pair(Bro):
    name = 'pair'
    description = 'test bro'
    spells = (dev.spells.run_pr, dev.spells.land, dev.spells.follow_pr)

  class RunPrOnly(Bro):
    name = 'run-pr-only'
    description = 'test bro'
    spells = (dev.spells.run_pr, dev.spells.land)

  assert {'run-pr', 'follow-pr'} <= set(Pair().spell_paths)
  with pytest.raises(ValueError, match="requires spell 'follow-pr'"):
    RunPrOnly()


def test_follow_pr_requires_landing_for_terminal_bookkeeping():
  class Incomplete(Bro):
    name = 'incomplete'
    description = 'test bro'
    spells = (dev.spells.follow_pr,)

  with pytest.raises(ValueError, match="requires spell 'land'"):
    Incomplete()


@pytest.mark.parametrize('channel', ['github', 'talk', 'local'])
@pytest.mark.parametrize('landing', ['merge', 'push'])
@pytest.mark.parametrize('harness', ['bro', 'claude'])
def test_delivery_routes_select_only_their_channel_and_method(
  channel, landing, harness, monkeypatch
):
  publish(monkeypatch, targets=('eyebro',))
  body = _TrackerDev().get_spell_body(
    'run-pr', harness=harness, hold='unattended', choices={'channel': channel, 'landing': landing}
  )
  channel_markers = {
    'github': '### GitHub review',
    'talk': '### Talk review',
    'local': '### Local review',
  }
  for name, marker in channel_markers.items():
    assert (marker in body) == (name == channel)
  method_marker = 'For push checks, run `check-head'
  assert (method_marker in body) == (landing == 'push')
  assert len(body) <= BYTE_LIMIT


def test_run_pr_defaults_select_the_github_merge_route():
  bro = _TrackerDev()
  assert bro.get_spell_body('run-pr', harness='bro', hold='unattended') == bro.get_spell_body(
    'run-pr', harness='bro', hold='unattended', choices={'channel': 'github', 'landing': 'merge'}
  )


def test_follow_pr_serves_the_replay_watch_within_one_window():
  body = _TrackerDev().get_spell_body('follow-pr', harness='bro', hold='unattended')
  assert '## Arm replay and reconcile' in body
  assert len(body) <= BYTE_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize('persona', [Dev, Eyebro, Lead])
async def test_provider_adoption_page_reaches_the_man_tool(persona, monkeypatch, tmp_path):
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
  with closing(persona()) as bro:
    servers = bro.assemble(harness='claude', hold='attended')
    manual = next(server for server in servers if server.namespace == 'man-source')
    [read] = await manual.list_tools()
    assert 'llm-providers' in read.parameters['properties']['topic']['enum']
    assert await read.call({'topic': 'llm-providers'}) == llm_providers.read()
    assert llm_providers.read() == llm_providers._path.read_text()
