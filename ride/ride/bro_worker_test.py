from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

import ride.bro_worker as bro_worker
from bro.authority.patterns import Descriptor
from bro.authority.records import dump_request
from bro.authority.rules import Mission, Prepared
from bro.base.host_config import Attachment
from bro.bro import BaseBro
from bro.broker.brotocol import request
from bro.worker_types import Expect, LaunchDenied, PeerDescription, Spawn
from bro.workspace.paths import CONTAINER_ARTIFACTS_ROOT
from ride.authority_test_helper import mission, reading
from ride.bro_worker import BroFacts, BroType, Placement, SummonLaunchSpec
from ride.launch_control import LaunchControl
from ride.workspace.metadata import Isolation


@pytest.fixture(autouse=True)
def installed_dev(register_test_bros):
  class Dev(BaseBro):
    name = 'dev'

  register_test_bros(Dev)


class Peers:
  def attribution_for_mission(self, journal, mission):
    return {'workspace': 'ws', 'type': 'bro', 'bro': 'bro-dev', 'trail_id': 'trail'}


class Host:
  def __init__(self, source=None):
    self.reading = source or reading(config={'defaults': {'summon-depth': 2}})
    self.workspace = SimpleNamespace(
      name='ws',
      metadata=SimpleNamespace(repo=None),
      repository=None,
    )
    self.peers = Peers()
    self.journal = MagicMock()
    self.session_env = {'ONE': '1'}
    self.summon_spawner = object()
    self.artifacts = MagicMock()


def _owner(tmp_path: Path, *, type='bro', grant=('@dev',), revoke=(), depth=0, source=None):
  return PeerDescription(
    mission='root',
    workspace='ws',
    tree=tmp_path,
    type=type,
    bro='bro-dev' if type == 'bro' else None,
    mission_record=mission(grant=grant, revoke=revoke, source=source),
    member=None,
    expected=False,
    artifact_view=PurePosixPath(CONTAINER_ARTIFACTS_ROOT),
    published_ports=(),
    depth=depth,
    extension=BroFacts('bro-dev') if type == 'bro' else None,
  )


def _request(tmp_path, *, host=None, owner=None, **args):
  host = host or Host()
  control = LaunchControl(
    ride='ws',
    types={'bro': BroType(host)},
    peers=host.peers,
    journal=host.journal,
    audit_file=tmp_path / 'audit.jsonl',
    runtime_bundle=MagicMock(),
    session_env={},
    worker_container_spawner=MagicMock(),
    reading=host.reading,
  )
  message = request('launch', {'type': 'bro', 'target': 'dev', 'prompt': 'work', **args})
  admitted, _ = control._request(message, owner or _owner(tmp_path, source=host.reading))
  return admitted


def test_owned_uninstalled_summon_default_refuses_the_reading(monkeypatch):
  with pytest.raises(ValueError, match='summon-harness.*not-installed'):
    reading(config={'defaults': {'summon-harness': 'not-installed'}})


def test_talk_widens_the_bro_default(tmp_path):
  worker = BroType(Host())
  assert worker.talk(_request(tmp_path, talk=['owner.say'])) == frozenset(
    {'worker.say', 'owner.say'}
  )


def test_spawn_launch_carries_the_authorized_child(tmp_path):
  host = Host()
  admitted = _request(
    tmp_path,
    host=host,
    hold='guided',
    **{'pass': ['github+work']},
    owner=_owner(tmp_path, grant=('@dev', 'bro.cred=github+work'), source=host.reading),
    share=['sha256:' + 'a' * 64 + '/report.md'],
  )
  run = BroType(host).launch(admitted)
  assert isinstance(run, Spawn)
  assert run.spawner is host.summon_spawner
  assert run.extension == BroFacts(bro='dev', placement=Placement('start', Isolation.BOXED))
  launch = cast(SummonLaunchSpec, run.launch)
  assert launch.record is admitted.record
  assert launch.record.descriptor.values['cred.github'] == 'work'
  assert launch.target == 'dev'
  assert launch.parent == 'ws'
  assert launch.parent_tree == tmp_path
  assert launch.hold == 'guided'
  assert launch.share == ('sha256:' + 'a' * 64 + '/report.md',)
  assert launch.summoner == {'trail_id': 'trail'}
  assert launch.env == {'ONE': '1'}


def test_manual_launch_returns_the_type_owned_pending_extension(tmp_path):
  host = Host()
  admitted = _request(
    tmp_path,
    host=host,
    manual=True,
    into='feature',
    **{'pass': ['github+work']},
    owner=_owner(tmp_path, grant=('@dev', 'bro.cred=github+work'), source=host.reading),
  )
  run = BroType(host).launch(admitted)
  assert isinstance(run, Expect)
  assert isinstance(admitted.record, Prepared)
  assert run.pending == {
    'target': 'dev',
    'prompt': 'work',
    'request': dump_request(admitted.record.request, Descriptor('bro', {})),
    'root': 'ws',
    'summoner': {'trail_id': 'trail'},
    'repo': None,
    'into': 'feature',
  }
  assert cast(BroFacts, run.extension).placement == Placement('start', None)


@pytest.mark.parametrize(
  ('args', 'message'),
  [
    ({'target': ''}, 'bro.target'),
    ({'prompt': ''}, 'non-empty string'),
    ({'unknown': True}, 'unknown bro fields'),
    ({'placement': 'merge'}, 'bro.placement'),
    ({'grant': [None]}, 'list of entries'),
    ({'step_id': -1}, 'non-negative int'),
    ({'index': 0}, 'requires step_id'),
    ({'into': ''}, 'non-empty git ref'),
  ],
)
def test_malformed_bro_arguments_are_denied(tmp_path, args, message):
  with pytest.raises(LaunchDenied, match=message):
    BroType(Host()).launch(_request(tmp_path, **args))


def test_owner_of_another_type_may_summon_with_authority(tmp_path):
  run = BroType(Host()).launch(_request(tmp_path, owner=_owner(tmp_path, type='test')))
  assert isinstance(run, Spawn)


def test_depth_cap_is_enforced_at_the_host_boundary(tmp_path):
  with pytest.raises(LaunchDenied, match='depth cap'):
    _request(tmp_path, owner=_owner(tmp_path, depth=2))


def test_target_requires_use_authority(tmp_path):
  with pytest.raises(LaunchDenied, match='use rule: @dev'):
    _request(tmp_path, owner=_owner(tmp_path, grant=()))


def test_unmarked_placement_defaults_to_boxed_without_using_authority(tmp_path):
  with pytest.raises(LaunchDenied, match='use rule: bro.placement=boxed'):
    _request(
      tmp_path,
      owner=_owner(
        tmp_path, grant=('@dev', 'bro.placement=unboxed'), revoke=('bro.placement=boxed',)
      ),
    )


def test_join_needs_use_authority_and_inherits_isolation(tmp_path):
  with pytest.raises(LaunchDenied, match='use rule: bro.placement=join'):
    _request(tmp_path, placement='join')
  admitted = _request(
    tmp_path, placement='join', owner=_owner(tmp_path, grant=('@dev', 'bro.placement=join'))
  )
  run = BroType(Host()).launch(admitted)
  assert cast(BroFacts, run.extension).placement == Placement('join', None)
  assert cast(SummonLaunchSpec, cast(Spawn, run).launch).isolation is None


def test_explicit_unboxed_placement_needs_use_authority(tmp_path):
  with pytest.raises(LaunchDenied, match='use rule: bro.placement=unboxed'):
    _request(tmp_path, placement='unboxed')


def test_gifts_require_give_authority(tmp_path):
  with pytest.raises(LaunchDenied, match='gift rule'):
    _request(tmp_path, grant=['@reviewer'])


def test_redundant_covered_grant_is_accepted(tmp_path):
  run = BroType(Host()).launch(
    _request(tmp_path, grant=['@reviewer'], owner=_owner(tmp_path, grant=('@dev', '@reviewer')))
  )
  assert isinstance(run, Spawn)


def test_admitted_record_controls_lowering_not_the_starters_authority(tmp_path):
  admitted = _request(tmp_path)
  admitted = replace(admitted, owner=_owner(tmp_path, grant=(), revoke=('bro',)))
  assert isinstance(BroType(Host()).launch(admitted), Spawn)


def test_pass_option_is_checked_before_credential_presence(tmp_path):
  with pytest.raises(LaunchDenied, match=r'use rule: bro.cred=github\+absent'):
    _request(tmp_path, **{'pass': ['github+absent']})


@pytest.mark.parametrize(
  ('field', 'kind', 'option'), [('grant', 'github', 'add'), ('revoke', 'openai', 'drop')]
)
def test_credential_kind_overrides_reach_the_child_descriptor(tmp_path, field, kind, option):
  admitted = _request(
    tmp_path, **{field: [kind]}, owner=_owner(tmp_path, grant=('@dev', 'bro.cred=github+'))
  )
  assert isinstance(admitted.record, Mission)
  assert kind in admitted.record.descriptor.values[option]


def test_a_gift_of_an_absent_instance_is_admitted_without_reading_its_material(tmp_path):
  admitted = _request(
    tmp_path,
    grant=['bro.cred=github+absent'],
    owner=_owner(tmp_path, grant=('@dev', 'bro.cred=github+absent')),
  )
  assert isinstance(BroType(Host()).launch(admitted), Spawn)


def test_harness_and_llm_choices_do_not_answer_to_the_summoners_credentials(tmp_path):
  admitted = _request(tmp_path, harness='claude', llm='anthropic:claude-opus-5')
  launch = cast(SummonLaunchSpec, cast(Spawn, BroType(Host()).launch(admitted)).launch)
  assert launch.harness == 'claude'
  assert launch.record.descriptor.values['provider'] == 'anthropic'


def test_an_incompatible_harness_and_llm_are_denied_before_acceptance(tmp_path):
  with pytest.raises(LaunchDenied, match="the claude harness cannot run provider 'openai'"):
    _request(tmp_path, harness='claude', llm='openai:gpt-6.1-sol')


def _host_recipes(recipe):
  source = reading(config={'homes': {'test-home': {'bros': {'dev': {'llm': {'bro': recipe}}}}}})
  return Host(replace(source, attachment=Attachment(path='/proj')))


def test_a_spawned_summon_refuses_a_configured_provider_its_harness_cannot_run(tmp_path):
  host = _host_recipes('anthropic:claude-opus-5-5,effort=high,fast_mode=true')
  with pytest.raises(LaunchDenied, match='cannot run provider'):
    _request(tmp_path, host=host)


def test_a_spawned_summon_runs_its_configured_recipe(tmp_path):
  host = _host_recipes('openai:gpt-5.6-terra')
  assert isinstance(BroType(host).launch(_request(tmp_path, host=host)), Spawn)


def test_a_manual_summon_leaves_the_recipe_to_the_operators_launch(tmp_path):
  host = _host_recipes('anthropic:claude-opus-5-5,effort=high,fast_mode=true')
  assert isinstance(BroType(host).launch(_request(tmp_path, host=host, manual=True)), Expect)


def test_audit_fields_are_projected_from_bro_facts(tmp_path):
  worker = BroType(Host())
  run = worker.launch(_request(tmp_path))
  assert worker.audit_fields(run.extension) == {
    'placement': {'party': 'start', 'isolation': 'boxed'},
    'target': 'dev',
  }


def test_a_bro_launch_requires_an_admitted_record(tmp_path):
  admitted = replace(_request(tmp_path), record=None)
  with pytest.raises(ValueError, match='requires an admitted record'):
    BroType(Host()).launch(admitted)


def test_spawn_records_are_missions(tmp_path):
  assert isinstance(_request(tmp_path).record, Mission)


def test_manual_preparation_publishes_the_bro_default_talk(tmp_path):
  from bro.summon import DEFAULT_TALK

  host = Host()
  request = _request(tmp_path, host=host, manual=True)
  assert BroType(host).talk(request) == DEFAULT_TALK


@pytest.mark.parametrize('passthrough', [False, True])
def test_summon_admission_and_join_command_forward_passthrough(tmp_path, passthrough):
  text = 'openai:gpt-7' + ('+passthrough' if passthrough else '') + ',reasoning_effort=high'
  if not passthrough:
    with pytest.raises(LaunchDenied, match='gpt-7.*passthrough'):
      _request(tmp_path, llm=text)
    return
  host = Host()
  admitted = _request(tmp_path, host=host, llm=text)
  launch = cast(SummonLaunchSpec, cast(Spawn, BroType(host).launch(admitted)).launch)
  spec = bro_worker._child_session_spec(launch, 'child', None)
  assert spec.recipe.passthrough
  assert spec.recipe.model == 'gpt-7'
  command = bro_worker._joined_ride_command(launch)
  assert '--llm ' + text in command
  assert '--model' not in command and '--passthrough' not in command
