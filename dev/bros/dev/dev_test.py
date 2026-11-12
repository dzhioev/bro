import re
import shlex
import subprocess
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
from bro.spells import build_spell_server, load_spell
from bro.workflow import land_push
from bro.workflow.git_test_helper import repository as repository
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

  native = bro.get_spell_body('run-pr', harness=get_harness('bro'), hold='unattended')
  claude = bro.get_spell_body('run-pr', harness=get_harness('claude'), hold='unattended')
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
  assert 'check-head --base <base>' in _section(body, '### Pre-review check')
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


def _section(text, heading):
  level = len(heading.split()[0])
  match = re.search(rf'(?ms)^{re.escape(heading)}\n(.*?)(?=^#{{1,{level}}} |\Z)', text)
  assert match is not None, heading
  return match[1].strip()


async def _delivery_call(arguments, harness):
  with closing(_TrackerDev()) as bro:
    spell = load_spell('run-pr', bro.spell_paths['run-pr'])
    server = build_spell_server(bro, harness=harness, hold='unattended')
    tool = next(tool for tool in await server.list_tools() if tool.name == spell.name)
    text = await tool.call(arguments)
  assert isinstance(text, str)
  body, argument_text = text.rsplit('\n# Arguments\n', 1)
  bound = dict(line.split(': ', 1) for line in argument_text.strip('\n').splitlines())
  assert arguments.items() <= bound.items()
  assert len(text) <= BYTE_LIMIT
  return spell, body


@pytest.mark.asyncio
@pytest.mark.parametrize('harness', ['bro', 'claude'])
@pytest.mark.parametrize('channel', ['github', 'talk', 'local'])
@pytest.mark.parametrize('lander', ['summon', 'coordinator', 'others'])
async def test_lander_handovers_reach_the_called_surface(monkeypatch, harness, channel, lander):
  publish(monkeypatch, targets=('eyebro', 'dev'))
  spell, body = await _delivery_call({'channel': channel, 'lander': lander}, harness)
  pattern = rf'(?ms)^- `lander={lander}`:.*?(?=^- `lander=|^A delegated lander)'
  declared = re.search(pattern, spell.body)
  delivered = re.search(pattern, body)
  assert declared is not None
  assert delivered is not None
  assert delivered[0] == declared[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('harness', ['bro', 'claude'])
@pytest.mark.parametrize(
  'arguments',
  [
    {'pr': 'https://github.com/owner/project/pull/42'},
    {
      'pr': 'https://github.com/owner/project/pull/42',
      'channel': 'talk',
      'landing': 'push',
      'reviewer': 'summon',
      'approver': 'summoner',
      'gates': 'verdict,checks,approval',
      'base': 'upstream/integration/topic',
    },
  ],
  ids=['without-delivery-line', 'recorded-delivery-choices'],
)
async def test_resume_entry_reaches_default_and_recorded_channel_renderings(
  monkeypatch, harness, arguments
):
  publish(monkeypatch, targets=('eyebro',))
  spell, body = await _delivery_call(arguments, harness)
  heading = '## Resume with `pr`'
  assert _section(body, heading) == _section(spell.body, heading)
  channel = arguments.get('channel', 'github')
  assert f'### {channel.title() if channel != "github" else "GitHub"} review' in body


@pytest.mark.asyncio
@pytest.mark.parametrize('harness', ['bro', 'claude'])
@pytest.mark.parametrize('channel', ['github', 'talk', 'local'])
@pytest.mark.parametrize('landing', ['merge', 'push'])
async def test_successor_and_delta_routes_survive_every_delivery_rendering(
  monkeypatch, harness, channel, landing
):
  publish(monkeypatch, targets=('eyebro',))
  spell, body = await _delivery_call({'channel': channel, 'landing': landing}, harness)
  heading = '### Failures and successors'
  assert _section(body, heading) == _section(spell.body, heading)


@pytest.mark.asyncio
@pytest.mark.parametrize('harness', ['bro', 'claude'])
@pytest.mark.parametrize('channel', ['github', 'talk', 'local'])
@pytest.mark.parametrize('gates', ['verdict,checks', 'checks', ''])
async def test_plan_refusals_and_no_verdict_bypass_survive_channel_selection(
  monkeypatch, harness, channel, gates
):
  publish(monkeypatch, targets=('eyebro',))
  spell, body = await _delivery_call({'channel': channel, 'gates': gates}, harness)
  for heading in ('### Resolve the base and gates', '### Check the plan'):
    assert _section(body, heading) == _section(spell.body, heading)


@pytest.mark.parametrize('rewrite', ['append', 'fold', 'rebase'])
def test_local_started_review_delivers_changed_heads_with_the_rendered_bundle(
  repository, tmp_path, rewrite
):
  previous = repository.git('rev-parse', 'HEAD')
  reviewer = tmp_path / 'reviewer'
  subprocess.run(
    ['git', 'clone', '-q', '--no-local', str(repository.path), str(reviewer)], check=True
  )
  subprocess.run(['git', '-C', str(reviewer), 'remote', 'remove', 'origin'], check=True)
  repository.git('remote', 'remove', 'origin')
  repository.commit('fix', 'review fix\n')
  if rewrite == 'fold':
    repository.git('reset', '--soft', 'main')
    repository.git('commit', '-qm', 'fold the reviewed change')
  elif rewrite == 'rebase':
    repository.git('checkout', '-q', 'main')
    repository.commit('base-change', 'base moved\n')
    repository.git('checkout', '-q', 'feature')
    repository.git('rebase', 'main')
  head = repository.git('rev-parse', 'HEAD')
  body = _TrackerDev().get_spell_body(
    'run-pr', harness='bro', hold='unattended', choices={'channel': 'local', 'landing': 'push'}
  )
  [bundle_command] = [line.strip() for line in body.splitlines() if 'git bundle create ' in line]
  command = shlex.split(
    bundle_command.replace('<last delivered head>', previous).replace('<head>', head)
  )
  (repository.path / '.git' / 'review').mkdir()
  subprocess.run(command, check=True)
  bundle = repository.path / command[3]
  for arguments in (('bundle', 'verify', str(bundle)), ('fetch', str(bundle), 'HEAD')):
    subprocess.run(['git', '-C', str(reviewer), *arguments], check=True)
  fetched = subprocess.check_output(
    ['git', '-C', str(reviewer), 'rev-parse', 'FETCH_HEAD'], text=True
  ).strip()
  assert fetched == head
  assert (
    subprocess.check_output(['git', '-C', str(reviewer), 'show', f'{head}:fix'], text=True)
    == 'review fix\n'
  )
  assert not repository.git('remote')
  assert not repository.git('status', '--porcelain')
  assert land_push.land_push('refs/heads/main', head, None, 0, []) is None
  assert repository.git('rev-parse', 'main') == head


def test_joined_review_observes_clean_turns_before_a_local_landing(repository):
  repository.git('remote', 'remove', 'origin')
  body = Eyebro().get_spell_body(
    'review', harness='bro', hold='unattended', choices={'channel': 'local'}
  )
  [command] = re.findall(r'`(git --no-optional-locks [^`]+)`', body)
  assert subprocess.check_output(shlex.split(command), text=True) == ''
  (repository.path / 'feature').write_text('author is answering the round\n')
  assert subprocess.check_output(shlex.split(command), text=True).strip() == 'M feature'
  head = repository.commit('feature', 'author answered the round\n')
  assert subprocess.check_output(shlex.split(command), text=True) == ''
  assert land_push.land_push('refs/heads/main', head, None, 0, []) is None
  assert repository.git('rev-parse', 'main') == head
  assert not repository.git('remote')


def test_talk_round_fetches_the_delivered_branch_by_its_base_url(repository, tmp_path):
  reviewer = tmp_path / 'reviewer'
  subprocess.run(['git', 'clone', '-q', str(repository.remote), str(reviewer)], check=True)
  subprocess.run(
    ['git', '-C', str(reviewer), 'remote', 'set-url', 'origin', '/absent/remote'], check=True
  )
  body = Eyebro().get_spell_body(
    'review', harness='bro', hold='unattended', choices={'channel': 'talk'}
  )
  [fetch] = [line for line in body.splitlines() if line.startswith('git fetch ')]
  command = shlex.split(
    fetch.replace('<base remote URL>', str(repository.remote)).replace(
      '<review branch>', 'review/topic'
    )
  )
  for content in ('first round\n', 'fixed round\n'):
    head = repository.commit('feature', content)
    repository.git('push', '-q', 'origin', 'HEAD:review/topic')
    subprocess.run(command, cwd=reviewer, check=True)
    assert (
      subprocess.check_output(
        ['git', '-C', str(reviewer), 'rev-parse', 'FETCH_HEAD'], text=True
      ).strip()
      == head
    )
    assert (
      subprocess.check_output(['git', '-C', str(reviewer), 'show', 'FETCH_HEAD:feature'], text=True)
      == content
    )
