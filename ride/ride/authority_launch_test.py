import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bro.authority.authority import Atom
from bro.authority.patterns import Descriptor
from bro.authority.records import (
  dump_authority,
  dump_request,
  dump_values,
  load_request,
  load_values,
)
from bro.authority.rules import Refusal, Request
from bro.authority.syntax import parse_layer
from bro.authority.values import Symbol
from bro.base import credentials, host_config
from bro.bro import BaseBro
from bro.broker.brotocol import request as message
from bro.harness import Harness
from bro.native.echo_declaration import LLMSpec
from bro.worker_types import installed_types
from ride.authority_launch import (
  credential_values,
  credentials_for,
  hydrate,
  mission_view,
  request,
  resume_request,
)
from ride.authority_reading import Configuration, HarnessReading, Installation, Reading
from ride.bro_worker import BroType
from ride.launch_control import LaunchControl
from ride.peer_facts import WorkerFacts


class Root(BaseBro):
  name = 'root'


class Browser(BaseBro):
  name = 'browser'
  may_launch = ('webview',)


@pytest.fixture
def source(monkeypatch, tmp_path):
  kinds = frozenset({'cookies', 'github', 'trails'})
  registry = {
    name: credentials.CredentialKind(name=name, description=name, literal=True) for name in kinds
  }
  monkeypatch.setattr(credentials, 'default_registry', lambda: registry)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path / 'store'))
  driver = Harness('probe')
  driver.llm_providers = ('echo',)
  installed = Installation(
    {'root': Root(credential_presence=kinds), 'browser': Browser(credential_presence=kinds)},
    {'probe': HarnessReading(driver, {'root': LLMSpec(), 'browser': LLMSpec()})},
    kinds,
    {'echo': frozenset()},
    {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
  )
  return Reading(
    Configuration.parse({}),
    {},
    installed,
    host_config.Attachment(path='/project'),
    {'summon-harness': 'probe'},
  )


def start(source, target='root', **fields):
  requested, _ = request(
    BroType, {'target': target, **fields}, source, source.operator(), forward=False
  )
  return source.rules.start(requested, source.operator(), source)


def test_give_only_cookie_right_reaches_the_recipient_but_not_the_starter(source):
  source = replace(
    source,
    configuration=Configuration.parse(
      {
        'projects': {
          '/project': {
            'bros': {'root': {'grant': ['@browser', 'webview.cred=cookies+profile to @browser']}}
          }
        }
      }
    ),
  )
  root = start(source)
  requested, _ = request(
    BroType,
    {'target': 'browser', 'grant': ['webview.cred=cookies+profile']},
    source,
    root.authority,
  )
  child = source.rules.start(requested, root.authority, source)
  webview = Request('webview', {'cred.cookies': 'profile'})
  with pytest.raises(Refusal, match='use rule: webview:'):
    source.rules.start(webview, root.authority, source)
  launched = source.rules.start(webview, child.authority, source)
  assert launched.descriptor.values['cred.cookies'] == 'profile'
  view = mission_view(root)
  assert any('webview.cred=cookies+profile to ' in entry for entry in view['may_give'])


def test_request_records_round_trip_typed_values_and_forwarded_gifts(source):
  root = start(source, grant=['@browser', 'webview.cred=cookies+profile to @browser'])
  requested, _ = request(
    BroType,
    {'target': 'browser', 'grant': ['webview.cred=cookies+profile']},
    source,
    root.authority,
  )
  descriptor = source.rules.normalize(requested, source)
  recorded = json.loads(json.dumps(dump_request(requested, descriptor)))
  restored = load_request(recorded, source.syntax)
  child = source.rules.start(restored, root.authority, source)
  atom = Atom('webview', 'cred.cookies', 'profile')
  assert child.authority.evaluate(
    atom, Descriptor('webview'), source.installation.declarations
  ).use.matches(Descriptor('webview'))
  values = {
    'model': Symbol('default'),
    'literal': '~default',
    'talk': frozenset({'owner.say'}),
    'timeout': 5,
  }
  assert load_values(json.loads(json.dumps(dump_values(values)))) == values


def test_resume_operator_grant_replaces_overlapping_wildcard_cap(source):
  previous = start(source, grant=['@browser'])
  operator_cap = source.syntax.layer(parse_layer(revoke=['@*'])).cap
  spec = SimpleNamespace(
    cred=[],
    grant=['@browser'],
    revoke=[],
    limit=[],
    operator_gift={},
    operator_cap=dump_authority(operator_cap, previous.descriptor),
  )
  requested, gift, cap = resume_request(previous.request, spec, source)
  resumed = source.rules.start(requested, source.operator(), source)
  authority = resumed.authority.join(gift).meet(cap)
  browser = Descriptor('bro', {'target': 'browser'})
  assert authority.evaluate(
    Atom('bro', 'target', 'browser'), browser, source.installation.declarations
  ).use.matches(browser)
  root = Descriptor('bro', {'target': 'root'})
  assert authority.evaluate(
    Atom('bro', 'target', 'root'), root, source.installation.declarations
  ).use.is_empty


def test_credential_changes_replace_picks_and_drops_per_kind():
  original = {
    'cred.github': 'old',
    'cred.cookies': 'profile',
    'drop': frozenset({'trails'}),
    'add': frozenset({'github'}),
  }
  values = credential_values(original, {'cred.trails': 'local', 'drop': frozenset({'github'})})
  assert 'cred.github' not in values
  assert values['cred.cookies'] == 'profile'
  assert values['cred.trails'] == 'local'
  assert values['drop'] == {'github'}
  with pytest.raises(ValueError, match='bro.cred.github'):
    credential_values(original, {'cred.github': 'new', 'drop': frozenset({'github'})})


def test_hydration_uses_descriptor_picks_without_re_reading_policy(source, tmp_path, monkeypatch):
  store = Path(credentials.STORE_DIR)
  (store / 'creds').mkdir(parents=True)
  (store / 'creds' / 'github+picked.cred').write_text('picked-material')
  (store / credentials.STORE_FILE).write_text(json.dumps({'defaults': ['github+elsewhere']}))
  mission = start(source, **cast(dict[str, Any], {'pass': ['github+picked']}))
  monkeypatch.setattr(Reading, 'take', lambda *args, **kwargs: pytest.fail('a child reread policy'))
  scoped = credentials_for(mission)
  files, kinds = hydrate(scoped)
  assert files['creds/github+picked.cred'] == b'picked-material'
  assert kinds == {'github'}


def test_manual_commit_can_be_retried_before_claim_and_replaces_starter_picks(
  source, tmp_path, monkeypatch
):
  store = Path(credentials.STORE_DIR)
  (store / 'creds').mkdir(parents=True)
  (store / 'creds' / 'github+operator.cred').write_text('operator-material')
  (store / credentials.STORE_FILE).write_text('{}')
  from bro.openai.declaration import LLMSpec as OpenAIRecipe

  driver = source.installation.harnesses['probe']
  driver.harness.llm_providers = ('openai', 'echo')
  source = replace(
    source,
    installation=replace(
      source.installation,
      harnesses={
        'probe': replace(
          driver, recipes={name: OpenAIRecipe() for name in source.installation.bros}, llm_key=True
        )
      },
      provider_needs={'echo': frozenset(), 'openai': frozenset({'cookies'})},
    ),
  )
  root = start(source, grant=['@browser', 'bro.cred=github+starter'])
  requested, _ = request(
    BroType,
    {'target': 'browser', 'pass': ['github+starter'], 'manual': True},
    source,
    root.authority,
  )
  prepared = source.rules.prepare(requested, root.authority, source)
  facts = {
    'root': WorkerFacts(type='bro', workspace='root', record=root),
    'manual': WorkerFacts(type='bro', workspace=None, record=prepared, expected=True),
  }
  peers = SimpleNamespace(for_mission=lambda identifier: facts[identifier])
  journal = SimpleNamespace(
    records={'manual': SimpleNamespace(parent='root', state='accepted')},
    ancestry=lambda identifier: ('root',),
  )
  replies = []
  context = SimpleNamespace(journal=journal, reply=lambda peer, result: replies.append(result))
  worker = BroType(cast(Any, SimpleNamespace(depth_cap=3)))
  control = LaunchControl(
    ride='root',
    types={'bro': worker},
    peers=peers,
    journal=cast(Any, journal),
    audit_file=tmp_path / 'audit',
    runtime_bundle=object(),
    session_env={},
    worker_container_spawner=cast(Any, object()),
    reading=source,
  )
  control._prepared['manual'] = prepared
  monkeypatch.setattr('ride.pending_launch.claimed_workspace', lambda token: None)
  commit = message(
    'operator.commit',
    {
      'token': 'manual',
      'fields': {
        'harness': 'probe',
        'placement': 'unboxed',
        'hold': 'unattended',
        'timeout': None,
        'llm': 'echo',
        'pass': ['github+operator'],
      },
    },
  )
  control.commit(cast(Any, context), 'operator', commit)
  control.commit(cast(Any, context), 'operator', commit)
  assert [reply['outcome'] for reply in replies] == ['ok', 'ok']
  assert replies[0]['value']['descriptor'] == replies[1]['value']['descriptor']
  assert replies[0]['value']['commit'] == replies[1]['value']['commit']
  assert replies[1]['value']['descriptor']['values']['cred.github'] == 'operator'
  assert replies[1]['value']['descriptor']['values']['provider'] == 'echo'
  assert replies[1]['value']['descriptor']['values']['placement'] == 'unboxed'
  assert {'provider', 'placement'} <= set(replies[1]['value']['operator_values'])
  assert 'cred.github' in replies[1]['value']['operator_values']
  commit_id = replies[1]['value']['commit']
  claimed = {}
  writes = []
  monkeypatch.setattr('ride.pending_launch.claimed_workspace', lambda token: claimed.get(token))

  def claim(token, *, workspace):
    writes.append((token, workspace))
    claimed[token] = workspace

  monkeypatch.setattr('ride.pending_launch.claim', claim)
  claim_message = message(
    'operator.claim', {'token': 'manual', 'commit': commit_id, 'workspace': 'committed-child'}
  )
  control.claim(cast(Any, context), 'operator', claim_message)
  control.claim(cast(Any, context), 'operator', claim_message)
  assert [reply['outcome'] for reply in replies[-2:]] == ['ok', 'ok']
  assert writes == [('manual', 'committed-child')]
  assert facts['manual'].workspace == 'committed-child'


def test_host_expands_presets_under_the_reading_and_overlays_piece_values(source):
  source = replace(source, configuration=Configuration.parse({'llm': {'chosen': 'echo'}}))
  mapped = BroType.option_values({'target': 'root', 'llm': 'chosen', 'effort': 'high'}, source)
  assert mapped.values['provider'] == 'echo'
  assert mapped.values['effort'] == 'high'


def test_reading_and_ambient_config_refuse_retired_default_picks_consistently():
  value = {'defaults': {'creds': ['github+picked']}}
  for parse in (Configuration.parse, host_config.parse):
    with pytest.raises(ValueError, match=r'defaults.creds is retired'):
      parse(value)


@pytest.mark.parametrize(
  ('option', 'value', 'permission'),
  [
    ('placement', 'unboxed', 'bro.placement=unboxed'),
    ('hold', 'guided', 'bro.hold=guided'),
    ('timeout', None, 'bro.timeout=none'),
    ('harness', 'uninstalled', 'bro.harness=uninstalled'),
  ],
)
def test_manual_commit_checks_the_operators_fixed_option_values(
  source, tmp_path, option, value, permission
):
  source = replace(
    source, configuration=Configuration.parse({'operator': {'revoke': [permission]}})
  )
  root = start(source, grant=['@browser'])
  requested, _ = request(BroType, {'target': 'browser', 'manual': True}, source, root.authority)
  prepared = source.rules.prepare(requested, root.authority, source)
  facts = {
    'root': WorkerFacts(type='bro', workspace='root', record=root),
    'manual': WorkerFacts(type='bro', workspace=None, record=prepared, expected=True),
  }
  peers = SimpleNamespace(for_mission=lambda identifier: facts[identifier])
  journal = SimpleNamespace(
    records={'manual': SimpleNamespace(parent='root', state='accepted')},
    ancestry=lambda identifier: ('root',),
  )
  replies = []
  context = SimpleNamespace(journal=journal, reply=lambda peer, result: replies.append(result))
  worker = BroType(cast(Any, object()))
  control = LaunchControl(
    ride='root',
    types={'bro': worker},
    peers=peers,
    journal=cast(Any, journal),
    audit_file=tmp_path / 'audit',
    runtime_bundle=object(),
    session_env={},
    worker_container_spawner=cast(Any, object()),
    reading=source,
  )
  fields = {
    'harness': 'probe',
    'placement': 'boxed',
    'hold': 'unattended',
    'timeout': 30,
    option: value,
  }
  control.commit(
    cast(Any, context),
    'operator',
    message('operator.commit', {'token': 'manual', 'fields': fields}),
  )
  [reply] = replies
  assert reply['outcome'] == 'denied'
  assert f'use rule: {permission}:' in reply['error']
  assert facts['manual'].record is prepared


def test_manual_commit_can_replace_a_complete_subset_option(source, tmp_path):
  root = start(source, grant=['@browser'])
  requested, _ = request(
    BroType, {'target': 'browser', 'add': ['github'], 'manual': True}, source, root.authority
  )
  prepared = source.rules.prepare(requested, root.authority, source)
  facts = {
    'root': WorkerFacts(type='bro', workspace='root', record=root),
    'manual': WorkerFacts(type='bro', workspace=None, record=prepared, expected=True),
  }
  replies = []
  journal = SimpleNamespace(
    records={'manual': SimpleNamespace(parent='root', state='accepted')},
    ancestry=lambda identifier: ('root',),
  )
  context = SimpleNamespace(journal=journal, reply=lambda peer, result: replies.append(result))
  control = LaunchControl(
    ride='root',
    types={'bro': BroType(cast(Any, object()))},
    peers=SimpleNamespace(for_mission=facts.__getitem__),
    journal=cast(Any, journal),
    audit_file=tmp_path / 'audit',
    runtime_bundle=object(),
    session_env={},
    worker_container_spawner=cast(Any, object()),
    reading=source,
  )
  fields = {
    'harness': 'probe',
    'placement': 'boxed',
    'hold': 'unattended',
    'timeout': None,
    'add': [],
  }
  control.commit(
    cast(Any, context),
    'operator',
    message('operator.commit', {'token': 'manual', 'fields': fields}),
  )
  [reply] = replies
  assert reply['outcome'] == 'ok', reply
  assert reply['value']['request']['values']['add'] == []
  assert 'cred.github' not in reply['value']['descriptor']['values']


def test_repeated_operator_replacement_discards_superseded_structure_and_round_trips(source):
  from bro.authority.authority import Cap
  from bro.authority.records import load_authority
  from ride.authority_launch import replace_operator_layer

  descriptor = Descriptor('bro', {'target': 'browser'})
  gift = source.syntax.layer(parse_layer(grant=['bro.placement=join'])).gift
  cap = Cap()
  granted = source.syntax.layer(parse_layer(grant=['@browser']))
  revoked = source.syntax.layer(parse_layer(revoke=['@browser']))
  atom = Atom('bro', 'target', 'browser')
  types = source.installation.declarations
  stable = None
  for index in range(1500):
    layer = revoked if index % 3 == 1 else granted
    gift, cap = replace_operator_layer(gift, cap, layer.gift, layer.cap)
    saved = json.loads(
      json.dumps(
        {
          'gift': dump_authority(gift, descriptor),
          'cap': dump_authority(cap, descriptor),
        }
      )
    )
    gift = load_authority(saved['gift'], source.syntax)
    cap = load_authority(saved['cap'], source.syntax)
    assert gift.meet(cap).evaluate(atom, descriptor, types).use.matches(descriptor) is (
      index % 3 != 1
    )
    assert gift.evaluate(Atom('bro', 'placement', 'join'), descriptor, types).use.matches(
      descriptor
    )
    if index == 2:
      stable = saved
  assert {
    'gift': dump_authority(gift, descriptor),
    'cap': dump_authority(cap, descriptor),
  } == stable
