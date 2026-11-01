import json
import subprocess
from collections.abc import MutableMapping
from dataclasses import replace
from itertools import product
from typing import ClassVar, NoReturn, cast

import pytest

from bro.authority.authority import Atom, Authority
from bro.authority.oracle_test_helper import evaluate
from bro.authority.patterns import Descriptor
from bro.authority.rules import Refusal, Request
from bro.authority.syntax import parse_layer
from bro.base import credentials, host_config
from bro.base.condition import iff, when
from bro.bro import BaseBro, feature
from bro.harness import Harness
from bro.llm.llm import Recipe as BaseRecipe
from bro.mcp import MCPServerSpec, ToolLayer, creds
from bro.native.echo_declaration import Recipe
from bro.worker_types import installed_types
from bro.workspace.settings import HomeSettings
from ride.authority_launch import mission_view
from ride.authority_reading import Configuration, HarnessReading, Installation, Reading
from ride.authority_scope import configured_entries, configured_kinds, existence
from ride.bro_worker import BroType
from ride.repository import Repository


def unbuilt() -> NoReturn:
  raise AssertionError('a manifest must not build a server')


def layer(namespace, *required, optional=()):
  return ToolLayer(
    server_specs=(
      MCPServerSpec(namespace, build=unbuilt, needed_secrets=required, optional_secrets=optional),
    )
  )


class Parent(BaseBro):
  name = 'parent'
  tools: ClassVar = [layer('replacement', 'inherited')]


class Child(Parent):
  name = 'child'
  features: ClassVar = {'search': creds.contains('gate')}
  tools: ClassVar = [
    when(feature('search'), layer('replacement', 'active')),
    iff(creds.contains('gate'), layer('choice', 'active'), layer('choice', 'fallback')),
    layer('optional', optional=('optional',)),
  ]
  may_summon = ('other',)
  may_launch = ('webview',)


class Other(BaseBro):
  name = 'other'
  extra_secrets = ('other_key',)


@pytest.fixture
def installation(monkeypatch):
  kinds = frozenset(
    {
      'gate',
      'active',
      'fallback',
      'inherited',
      'optional',
      'other_key',
      'trails',
      'auth',
      'provider_key',
      'cookies',
    }
  )
  registry = {kind: credentials.CredentialKind(name=kind, description=kind) for kind in kinds}
  monkeypatch.setattr(credentials, 'default_registry', lambda: registry)
  monkeypatch.setattr(
    credentials, 'available', lambda name: pytest.fail('material probe during normalization')
  )
  native = Harness('native')
  native._llm_providers = ('echo',)
  auth = Harness('auth')
  auth._llm_providers = ('echo',)
  return Installation(
    {'child': Child(credential_presence=kinds), 'other': Other(credential_presence=kinds)},
    {
      'native': HarnessReading(native, {'child': Recipe(), 'other': Recipe()}, llm_key=True),
      'auth': HarnessReading(auth, {'child': Recipe(), 'other': Recipe()}, auth_kind='auth'),
    },
    kinds,
    {'echo': frozenset({'provider_key'})},
    {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
  )


def reading(installation, configuration=None, project=None, defaults=None, settings=None):
  return Reading(
    Configuration.parse(configuration or {}),
    defaults or {},
    installation,
    host_config.Attachment(path='/project', url='https://github.com/example/project.git'),
    project=project or {},
    settings=settings or HomeSettings(summon_harness='native'),
  )


def normalized(source, **values):
  return source.rules.normalize(Request('bro', {'target': 'child', **values}), source)


def test_defaults_cover_all_gate_outcomes_without_probing_material(installation):
  source = reading(installation)
  descriptor = normalized(source)
  names = {name.removeprefix('cred.') for name in descriptor.values if name.startswith('cred.')}
  assert {'active', 'fallback', 'inherited'} <= names
  assert 'provider_key' in names
  assert 'auth' not in names
  alternate = normalized(source, harness='auth')
  assert 'cred.auth' in alternate.values and 'cred.provider_key' not in alternate.values
  assert (
    source.installation.declarations['bro']
    .options['talk']
    .domain.contains(descriptor.values['talk'])
  )
  source.rules.use(Authority.top(), descriptor)


def test_only_selected_components_hydrate(installation):
  source = reading(installation)
  descriptor = normalized(source)
  present = frozenset({'gate', 'active', 'fallback', 'inherited', 'provider_key'})
  selected = existence(descriptor, source, present)
  assert 'active' in selected.required
  assert not {'fallback', 'inherited'} & selected.kinds
  selected = existence(descriptor, source, present - {'gate', 'active'})
  assert {'fallback', 'inherited'} <= selected.required
  assert 'active' not in selected.kinds
  with pytest.raises(ValueError, match='missing.*active'):
    existence(descriptor, source, present - {'active'})


def test_optional_pick_absence_and_explicit_instance(installation):
  source = reading(installation)
  descriptor = normalized(source)
  present = frozenset({'fallback', 'inherited', 'provider_key'})
  assert 'optional' not in existence(descriptor, source, present).kinds
  with pytest.raises(ValueError, match='missing.*optional'):
    existence(descriptor, source, present, explicit=frozenset({'optional'}))
  picked = reading(installation, defaults={'optional': 'chosen'})
  with pytest.raises(ValueError, match='missing.*optional'):
    existence(normalized(picked), picked, present)


def test_add_drop_and_explicit_credentials(installation):
  source = reading(installation)
  descriptor = normalized(source, add=frozenset({'cookies'}), drop=frozenset({'gate'}))
  assert 'cred.cookies' in descriptor.values and 'cred.gate' not in descriptor.values
  descriptor = normalized(source, **{'cred.cookies': 'profile'})
  assert descriptor.values['cred.cookies'] == 'profile'
  with pytest.raises(Refusal, match=r'cred.cookies.*bro.drop'):
    normalized(source, drop=frozenset({'cookies'}), **{'cred.cookies': 'profile'})
  for value in (False, 1.5, 0, -1, 'auto'):
    with pytest.raises(Refusal, match='domain check'):
      normalized(source, timeout=value)
  normalized(source, timeout='none')


def test_uninstalled_target_and_harness_fail_only_at_existence(installation):
  source = reading(installation)
  for values in ({'target': 'uninstalled'}, {'harness': 'uninstalled'}):
    descriptor = normalized(source, **values)
    assert 'model' not in descriptor.values
    with pytest.raises(ValueError, match='not installed'):
      existence(descriptor, source, frozenset())
  with pytest.raises(Refusal, match='target must be set'):
    source.rules.normalize(Request('bro'), source)
  for target in ('Has-Caps', 'a_b', ''):
    with pytest.raises(Refusal, match='domain check'):
      normalized(source, target=target)


def test_configured_instances_are_target_harness_provider_scoped(installation):
  source = reading(
    installation,
    {
      'projects': {
        '/project': {
          'bros': {
            'child': {'creds': ['active+child-secret']},
            'other': {'creds': ['other_key+other-secret']},
          }
        }
      }
    },
  )
  descriptor = normalized(source)
  base = installation.declarations['bro'].base(descriptor, source)
  assert len(configured_entries(source)) == len(installation.credential_kinds) + 2
  atom = Atom('bro', 'cred.active', 'child-secret')
  pair = base.evaluate(atom, descriptor, installation.declarations)
  assert pair.use.matches(descriptor)
  assert pair.give.is_empty
  for target, harness, provider in product(
    ('child', 'other'), ('native', 'auth'), ('echo', 'other-provider')
  ):
    probe = Descriptor('bro', {'target': target, 'harness': harness, 'provider': provider})
    actual = base.evaluate(atom, probe, installation.declarations).use.matches(probe)
    assert actual == (target == 'child' and provider == 'echo')
  other_atom = Atom('bro', 'cred.other_key', 'other-secret')
  assert not base.evaluate(other_atom, descriptor, installation.declarations).use.matches(
    descriptor
  )
  with pytest.raises(Refusal, match='use rule'):
    source.rules.use(base, normalized(source, **{'cred.active': 'foreign'}))


def test_bases_match_finite_evaluator_and_layer_caps_before_grants(installation):
  source = reading(
    installation,
    {
      'defaults': {'revoke': ['webview']},
      'projects': {
        'https://github.com/example/project.git': {'grant': ['webview']},
        '/project': {'revoke': ['@other'], 'bros': {'child': {'grant': ['@other']}}},
      },
    },
  )
  descriptor = normalized(source)
  mission = source.rules.start(Request('bro', {'target': 'child'}), Authority.top(), source)
  atoms = [
    Atom('bro'),
    Atom('bro', 'placement', 'boxed'),
    Atom('webview'),
    Atom('bro', 'target', 'other'),
    Atom('bro', 'timeout', 'none'),
    Atom('bro', 'cred.active', ''),
    Atom('bro', 'cred.active', 'fresh'),
  ]
  for atom in atoms:
    pair = mission.base.evaluate(atom, descriptor, installation.declarations)
    for probe in (descriptor, Descriptor('webview'), Descriptor('bro', {'target': 'other'})):
      assert pair.use.matches(probe) == evaluate(mission.base, atom, descriptor, probe, give=False)
      assert pair.give.matches(probe) == evaluate(mission.base, atom, descriptor, probe, give=True)
  assert mission.base.evaluate(Atom('webview'), descriptor, installation.declarations).use.matches(
    Descriptor('webview')
  )
  assert mission.base.evaluate(
    Atom('bro', 'target', 'other'), descriptor, installation.declarations
  ).use.matches(Descriptor('bro', {'target': 'other'}))
  assert not mission.base.evaluate(
    Atom('bro', 'target', 'unknown'), descriptor, installation.declarations
  ).use.matches(descriptor)
  source.rules.use(mission.base, normalized(source, target='other'))


def test_unknown_names_in_unapplied_layers_do_not_poison_launch(installation):
  source = reading(
    installation,
    {
      'projects': {
        '/elsewhere': {'grant': ['missing-type']},
        '/project': {'bros': {'other': {'creds': ['unknown+instance'], 'grant': ['missing-type']}}},
      }
    },
  )
  mission = source.rules.start(Request('bro', {'target': 'child'}), Authority.top(), source)
  from ride.authority_launch import mission_view

  assert mission_view(mission)['descriptor']['values']['target'] == 'child'
  with pytest.raises(ValueError, match='unknown'):
    source.rules.start(Request('bro', {'target': 'other'}), Authority.top(), source)


def test_operator_caps_and_layer_conflicts(installation):
  source = reading(installation, {'operator': {'revoke': ['@other']}})
  descriptor = normalized(source, target='other')
  with pytest.raises(Refusal, match='use rule'):
    source.rules.use(source.operator(), descriptor)
  bad = reading(installation, {'defaults': {'grant': ['@other'], 'revoke': ['@other']}})
  with pytest.raises(ValueError, match='grant and revoke'):
    bad.rules.start(Request('bro', {'target': 'child'}), Authority.top(), bad)


def test_reading_snapshots_whole_config_defaults_home_and_tree(
  tmp_path, installation, home_settings_file
):
  home_settings_file.write_text('summon-harness = "native"\ngrant = ["@other"]\n')
  host, store, project = tmp_path / 'host.json', tmp_path / 'store', tmp_path / 'project'
  store.mkdir()
  project.mkdir()
  host.write_text(
    json.dumps(
      {
        'defaults': {'grant': ['webview']},
        'operator': {'revoke': ['@other']},
        'llm': {'local': 'echo:echo'},
        'summon-depth': 7,
        'user': {'creds': ['cookies+operator']},
      }
    )
  )
  (store / 'creds.json').write_text(json.dumps({'defaults': ['cookies+first'], 'sources': {}}))
  (project / 'pyproject.toml').write_text(
    '[tool.bro]\nsummon-harness = "native"\ngrant = ["@other"]\n'
  )
  source = Reading.take(
    Repository(str(project), project), host=host, store=store, installation=installation
  )
  home_settings_file.write_text('broken')
  host.write_text('{broken')
  (store / 'creds.json').write_text('{broken')
  (project / 'pyproject.toml').write_text('[broken')
  assert source.picks('child')['cookies'] == 'first'
  assert source.configuration.raw['user']['creds'] == ('cookies+operator',)
  assert source.configuration.raw['llm']['local'] == 'echo:echo'
  assert source.summon_depth == 7
  assert source.home_policy.layer.grant
  normalized(source)
  with pytest.raises(TypeError):
    source.configuration.raw['llm']['new'] = 'echo:echo'
  with pytest.raises(TypeError):
    cast(MutableMapping[str, str], source.store_defaults)['cookies'] = 'new'


def test_reading_skips_unregistered_store_defaults_but_rejects_used_policy(tmp_path, installation):
  (tmp_path / 'creds.json').write_text(
    json.dumps({'defaults': ['cookies+work', 'consumer_only+host']})
  )
  source = Reading.take(host=tmp_path / 'absent', store=tmp_path, installation=installation)

  assert source.picks('child') == {'cookies': 'work'}
  request = Request('bro', {'target': 'child', 'harness': 'native'})
  source.rules.start(request, Authority.top(), source)
  configured = replace(
    source, home_policy=replace(source.home_policy, picks={'consumer_only': 'host'})
  )
  with pytest.raises(ValueError, match='unknown credential kind'):
    configured.rules.start(request, Authority.top(), configured)


@pytest.mark.parametrize(
  'defaults', [['consumer_only'], ['consumer_only+one', 'consumer_only+two']]
)
def test_reading_validates_unregistered_default_syntax(tmp_path, installation, defaults):
  (tmp_path / 'creds.json').write_text(json.dumps({'defaults': defaults}))

  with pytest.raises(ValueError):
    Reading.take(host=tmp_path / 'absent', store=tmp_path, installation=installation)


def test_home_settings_refuse_instance_entries(tmp_path, installation, home_settings_file):
  project = tmp_path / 'project'
  project.mkdir()
  for entry in ('bro.cred=cookies+profile', 'bro.cred=cookies+* to @child'):
    home_settings_file.write_text('grant = [' + json.dumps(entry) + ']')
    with pytest.raises(ValueError, match='credential instances'):
      Reading.take(
        Repository(str(project), project),
        host=tmp_path / 'absent',
        store=tmp_path / 'absent',
        installation=installation,
      )


def test_recipe_list_prefers_first_served_and_more_specific_replaces(installation):
  source = reading(
    installation,
    {
      'projects': {
        'https://github.com/example/project.git': {
          'bros': {'child': {'llm': ['anthropic:opus55:high', 'echo:echo', 'default']}}
        },
        '/project': {'bros': {'child': {'llm': ['echo:custom+passthrough']}}},
      }
    },
  )
  assert normalized(source).values['model'] == 'custom'
  assert source.attachment is not None
  less_specific = replace(source, attachment=host_config.Attachment(url=source.attachment.url))
  assert normalized(less_specific).values['provider'] == 'echo'
  assert normalized(less_specific).values['model'] == 'echo'
  with pytest.raises(ValueError):
    normalized(source, provider='anthropic')


def test_other_types_defaults_domains_and_empty_bases(installation):
  source = reading(installation)
  webview = source.rules.start(Request('webview'), Authority.top(), source)
  assert webview.base == Authority()
  source.rules.use(Authority.top(), webview.descriptor)
  with pytest.raises(Refusal, match='requires vnc'):
    source.rules.normalize(Request('webview', {'vnc_port': 2345}), source)
  with pytest.raises(Refusal, match='domain check'):
    source.rules.normalize(Request('webview', {'vnc': True, 'vnc_port': 1023}), source)
  source.rules.normalize(Request('webview', {'vnc': True, 'vnc_port': 2345}), source)
  job = source.rules.start(Request('benchmark'), Authority.top(), source)
  assert job.base == Authority()
  with pytest.raises(Refusal, match='domain check'):
    source.rules.normalize(Request('benchmark', {'timeout': 'none'}), source)


def test_url_attachment_reads_only_tree_sections_at_the_resolved_commit(
  tmp_path, installation, home_settings_file
):
  home_settings_file.write_text('summon-harness = "native"\ngrant = ["@other"]\n')
  subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
  project = tmp_path / 'pyproject.toml'
  project.write_text(
    '[tool.bro]\nsummon-harness = "ignored"\ngrant = ["invalid"]\n[tool.bro.example]\nvalue = "committed"'
  )
  subprocess.run(['git', '-C', str(tmp_path), 'add', 'pyproject.toml'], check=True)
  subprocess.run(
    [
      'git',
      '-C',
      str(tmp_path),
      '-c',
      'user.name=Test',
      '-c',
      'user.email=test@example.com',
      'commit',
      '-qm',
      'fixture',
    ],
    check=True,
  )
  commit = subprocess.check_output(
    ['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True
  ).strip()
  project.write_text('[tool.bro.example]\nvalue = "working"')
  source = Reading.take(
    Repository('https://github.com/example/project.git', tmp_path, commit),
    host=tmp_path / 'absent',
    store=tmp_path / 'absent',
    installation=installation,
  )
  assert source.summon_harness == 'native'
  assert source.home_policy.layer.grant and not source.home_policy.layer.revoke
  assert source.project['example']['value'] == 'committed'


def test_wire_mapping_separates_values_from_content_and_checks_sentinels(installation):
  source = reading(installation)
  worker = installed_types()['bro']
  mapped = worker.option_values(
    {
      'target': 'child',
      'prompt': 'hello',
      'talk': ['worker.question'],
      'timeout': None,
      'pass': ['cookies+profile'],
      'placement': 'unboxed',
    },
    source,
  )
  descriptor = source.rules.normalize(Request('bro', mapped.values), source)
  assert 'prompt' in mapped.content and 'prompt' not in descriptor.values
  assert descriptor.values['timeout'] == 'none'
  assert descriptor.values['placement'] == 'unboxed'
  assert descriptor.values['talk'] == frozenset({'worker.say', 'worker.question'})
  with pytest.raises(Refusal, match='use rule'):
    source.rules.use(source.syntax.layer(parse_layer(grant=['bro', '@child'])).gift, descriptor)
  for fields in (
    {'manual': True, 'timeout': 1},
    {'manual': True, 'placement': 'join'},
    {'placement': 'join', 'into': 'ref'},
    {'unknown': 'value'},
  ):
    with pytest.raises(ValueError):
      worker.option_values(fields, source)
  webview = installed_types()['webview'].option_values(
    {
      'cookies': 'profile',
      'vnc_port': None,
      'allowed_origins': ['https://example.com'],
      'timeout': None,
    },
    source,
  )
  web_descriptor = source.rules.normalize(Request('webview', webview.values), source)
  assert web_descriptor.values['vnc_port'] == 'auto'
  assert 'allowed_origins' in webview.content and 'allowed_origins' not in web_descriptor.values
  benchmark = installed_types()['benchmark'].option_values(
    {'config': 'job.toml', 'timeout': 42}, source
  )
  assert benchmark.content['config'] == 'job.toml'
  assert (
    source.rules.normalize(Request('benchmark', benchmark.values), source).values['timeout'] == 42
  )


def test_export_is_a_guarded_flag_that_needs_its_own_use_permission(installation):
  source = reading(installation)
  worker = installed_types()['bro']
  mapped = worker.option_values({'target': 'child', 'export': True}, source)
  descriptor = source.rules.normalize(Request('bro', mapped.values), source)
  assert descriptor.values['export'] is True
  guarded = frozenset({'export'})
  with pytest.raises(Refusal, match='use rule'):
    source.rules.use(
      source.syntax.layer(parse_layer(grant=['bro', '@child'])).gift,
      descriptor,
      options=guarded,
      start=False,
    )
  source.rules.use(
    source.syntax.layer(parse_layer(grant=['bro.export'])).gift,
    descriptor,
    options=guarded,
    start=False,
  )
  unset = worker.option_values({'target': 'child', 'export': False}, source)
  assert 'export' not in unset.values
  with pytest.raises(ValueError, match='export must be a flag'):
    worker.option_values({'target': 'child', 'export': 'yes'}, source)


def test_joint_commit_refuses_join_even_for_operator(installation):
  source = reading(installation)
  prepared = source.rules.prepare(Request('bro', {'target': 'child'}), Authority.top(), source)
  with pytest.raises(Refusal, match='joint validity check'):
    source.rules.commit(prepared, {'placement': 'join'}, Authority.top(), Authority.top())
  source.rules.commit(prepared, {'placement': 'unboxed'}, Authority.top(), Authority.top())


def test_manifest_union_excludes_impossible_choice_outcomes(installation):
  class NeedsGate(BaseBro):
    name = 'choice-needs-gate'
    tools: ClassVar = [iff(creds.contains('gate'), layer('required', 'gate', 'active'))]

  bro = NeedsGate(credential_presence=installation.credential_kinds)
  harness = installation.harnesses['native'].harness
  assert bro.possible_credential_kinds(harness, installation.credential_kinds) == {'gate', 'active'}
  with pytest.raises(ValueError, match='no condition'):
    bro.credential_manifest(harness, frozenset(), installation.credential_kinds)


def test_configured_lazy_entries_match_eager_at_prepare_and_commit(installation):
  from bro.authority.authority import Entry, Pair, Permission
  from bro.authority.patterns import Pattern, PatternSet

  source = reading(
    installation,
    {
      'projects': {
        '/project': {
          'bros': {
            'child': {'creds': ['active+child-secret']},
          }
        }
      }
    },
  )
  caller = source.rules.start(Request('bro', {'target': 'child'}), Authority.top(), source)
  passed = Request('bro', {'target': 'child', 'cred.active': 'child-secret'})
  prepared = source.rules.prepare(
    passed, caller.base.join(source.syntax.layer(parse_layer(grant=['@child'])).gift), source
  )
  declaration = installation.declarations['bro']
  pattern = Pattern(
    'bro',
    {
      name: declaration.options[name].domain.singleton(value)
      for name, value in {'target': 'child', 'harness': 'native', 'provider': 'echo'}.items()
    },
    frozenset({'target', 'harness', 'provider'}),
  )
  permission = Permission(
    'bro', 'cred.active', declaration.options['cred.active'].domain.singleton('child-secret')
  )
  eager = Authority((Entry(permission, Pair(PatternSet((pattern,)))),))
  lazy = Authority(
    tuple(entry for entry in configured_entries(source) if entry.permission == permission)
  )
  for values in (
    {},
    {'target': 'child'},
    {'target': 'child', 'harness': 'native'},
    {'target': 'child', 'harness': 'native', 'provider': 'echo'},
  ):
    descriptor = Descriptor('bro', {**values, 'cred.active': 'child-secret'})
    atom = Atom('bro', 'cred.active', 'child-secret')
    assert lazy.evaluate(atom, descriptor, installation.declarations).use.matches(
      descriptor, prepare=True
    )
    assert eager.evaluate(atom, descriptor, installation.declarations).use.matches(
      descriptor, prepare=True
    )
  source.rules.commit(
    prepared,
    {'harness': 'native'},
    caller.base.join(source.syntax.layer(parse_layer(grant=['@child'])).gift),
    Authority.top(),
  )
  with pytest.raises(Refusal, match='use rule'):
    source.rules.commit(
      prepared,
      {'target': 'other'},
      caller.base.join(source.syntax.layer(parse_layer(grant=['@child', '@other'])).gift),
      Authority.top(),
    )


def test_authorized_missing_target_reaches_existence(installation):
  source = reading(installation, {'defaults': {'grant': ['@uninstalled']}})
  caller = source.rules.start(Request('bro', {'target': 'child'}), Authority.top(), source)
  mission = source.rules.start(Request('bro', {'target': 'uninstalled'}), caller.base, source)
  assert not any(name.startswith('cred.') for name in mission.descriptor.values)
  with pytest.raises(ValueError, match='target.*not installed'):
    existence(mission.descriptor, source, frozenset())


def test_drop_wins_over_add_during_hydration(installation):
  source = reading(installation)
  descriptor = normalized(source, add=frozenset({'cookies'}), drop=frozenset({'cookies'}))
  selected = existence(descriptor, source, frozenset({'fallback', 'inherited', 'provider_key'}))
  assert 'cookies' not in selected.kinds


def test_explicit_presence_never_initializes_ambient_store(installation, monkeypatch):
  monkeypatch.setattr(credentials, 'default_store', lambda: pytest.fail('ambient store opened'))
  child = Child(
    credential_presence=installation.credential_kinds,
    credential_kinds=installation.credential_kinds,
  )
  assert child.possible_credential_kinds(
    installation.harnesses['native'].harness, installation.credential_kinds
  )


def test_model_alias_is_mapped_before_use_checks(installation):
  from bro.openai.declaration import Recipe as OpenAISpec

  native = Harness('native')
  native._llm_providers = ('openai',)
  installation = replace(
    installation,
    harnesses={
      'native': HarnessReading(
        native,
        {'child': OpenAISpec(reasoning_effort='medium'), 'other': OpenAISpec()},
        llm_key=False,
      )
    },
    provider_needs={'openai': frozenset()},
  )
  source = reading(installation)
  from bro.llm.providers import resolve_model

  canonical = resolve_model('openai', 'sol')
  starter = Authority.top().meet(
    source.syntax.layer(parse_layer(revoke=[f'bro.model={canonical}'])).cap
  )
  worker = installed_types()['bro']
  for model in ('sol', canonical):
    mapped = worker.option_values({'target': 'child', 'llm': f'openai:{model}'}, source)
    assert mapped.values['model'] == canonical
    with pytest.raises(Refusal, match='use rule'):
      source.rules.start(Request('bro', mapped.values), starter, source)
  with pytest.raises(ValueError, match='canonical model id'):
    normalized(source, provider='openai', model='sol')


def test_new_grammar_snapshot_does_not_open_the_ambient_store(tmp_path, monkeypatch):
  host = tmp_path / 'host.json'
  host.write_text(json.dumps({'operator': {'revoke': ['@other']}}))
  monkeypatch.setattr(credentials, 'default_store', lambda: pytest.fail('ambient store opened'))
  snapshot = Reading.take(host=host, store=tmp_path / 'empty')
  assert snapshot.configuration.operator.revoke
  assert snapshot.installation.declarations


def recipe_installation(installation: Installation, harness: str, spec: BaseRecipe) -> Installation:
  driver = Harness(harness)
  driver._llm_providers = (spec.TYPE,)
  return replace(
    installation,
    harnesses={harness: HarnessReading(driver, dict.fromkeys(installation.bros, spec))},
    provider_needs={spec.TYPE: frozenset()},
  )


def test_deferred_recipe_choices_are_checked_then_omitted(installation):
  from bro.anthropic.declaration import Recipe as AnthropicSpec
  from ride.authority_scope import DEFAULT, checked_recipe

  spec = AnthropicSpec(model=None, effort=None)
  source = reading(
    recipe_installation(installation, 'claude', spec),
    settings=HomeSettings(summon_harness='claude'),
  )
  request = Request('bro', {'target': 'child'})
  descriptor = source.rules.normalize(request, source)
  assert descriptor.values['model'] == DEFAULT and descriptor.values['effort'] == DEFAULT
  for option in ('model', 'effort'):
    cap = source.syntax.layer(parse_layer(revoke=[f'bro.{option}=~default'])).cap
    with pytest.raises(Refusal, match='use rule'):
      source.rules.start(request, Authority.top().meet(cap), source)
  source.rules.use(Authority.top(), descriptor)
  assert checked_recipe(request, descriptor, source) == spec
  literal_cap = source.syntax.layer(parse_layer(revoke=['bro.model=default'])).cap
  source.rules.use(Authority.top().meet(literal_cap), descriptor)
  explicit = Request(
    'bro',
    {
      'target': 'child',
      'provider': 'anthropic',
      'model': 'claude-opaque-new',
      'effort': 'high',
      'passthrough': True,
    },
  )
  explicit_descriptor = source.rules.normalize(explicit, source)
  cap = source.syntax.layer(parse_layer(revoke=['bro.model=~default', 'bro.effort=~default'])).cap
  source.rules.use(Authority.top().meet(cap), explicit_descriptor)


def test_opaque_literal_model_ids_do_not_collide_with_default_state(installation):
  from bro.openai.declaration import Recipe as OpenAISpec
  from ride.authority_scope import DEFAULT, checked_recipe

  source = reading(recipe_installation(installation, 'native', OpenAISpec()))
  for model in ('default', '~default', 'vendor-private-variant'):
    request = Request(
      'bro', {'target': 'child', 'provider': 'openai', 'model': model, 'passthrough': True}
    )
    descriptor = source.rules.normalize(request, source)
    assert descriptor.values['model'] == model and descriptor.values['model'] != DEFAULT
    source.rules.use(Authority.top(), descriptor)
    assert checked_recipe(request, descriptor, source).selection()['model'] == model
  with pytest.raises(ValueError, match='cannot defer model'):
    normalized(source, provider='openai', model=DEFAULT)


@pytest.mark.parametrize('effort', ['none', 'minimal', None])
def test_provider_efforts_reconstruct_exactly_and_remain_governed(installation, effort):
  from bro.authority.syntax import render_values
  from bro.openai.declaration import Recipe as OpenAISpec
  from ride.authority_scope import checked_recipe

  standing = OpenAISpec(
    model='vendor-private-variant',
    passthrough=True,
    reasoning_effort=effort,
    service_tier='flex',
    compact_threshold=12345,
  )
  source = reading(recipe_installation(installation, 'native', standing))
  request = Request('bro', {'target': 'child'})
  descriptor = source.rules.normalize(request, source)
  domain = source.installation.declarations['bro'].options['effort'].domain
  cap = source.syntax.layer(
    parse_layer(
      revoke=['bro.effort=' + render_values(domain.singleton(descriptor.values['effort']))]
    )
  ).cap
  with pytest.raises(Refusal, match='use rule'):
    source.rules.use(Authority.top().meet(cap), descriptor)
  source.rules.use(Authority.top(), descriptor)
  assert checked_recipe(request, descriptor, source) == standing
  value = 'minimal' if effort is None else effort
  explicit = Request(
    'bro',
    {
      'target': 'child',
      'provider': 'openai',
      'model': 'vendor-private-variant',
      'effort': value,
      'passthrough': True,
    },
  )
  explicit_descriptor = source.rules.normalize(explicit, source)
  source.rules.use(Authority.top(), explicit_descriptor)
  assert checked_recipe(explicit, explicit_descriptor, source).selection()['effort'] == value
  from bro.llm.providers import parse

  with pytest.raises(ValueError, match='effort level'):
    parse('openai:vendor-private-variant:' + value)


def test_cheatsheet_examples_use_shipped_configuration_and_permission_grammar():
  import re
  import shlex

  from bro import reference
  from bro.authority.syntax import Syntax

  kinds = frozenset(credentials.default_registry())
  syntax = Syntax(
    {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
    kinds,
  )
  text = (reference.DIRECTORY / 'permissions_cheatsheet.md').read_text()
  for language, body in re.findall(r'```(\w+)\n(.*?)```', text, re.DOTALL):
    if language == 'json':
      configuration = Configuration.parse(json.loads(body))
      syntax.layer(configuration.defaults.layer)
      syntax.layer(configuration.operator)
      for project in configuration.projects.values():
        syntax.layer(project.policy.layer)
        for policy in project.bros.values():
          syntax.layer(policy.layer)
    elif language == 'sh':
      for line in body.splitlines():
        words = shlex.split(line)
        for index, word in enumerate(words):
          if word in ('--grant', '--revoke', '--limit'):
            entries = {'grant': [], 'revoke': [], 'limit': []}
            entries[word[2:]] = [words[index + 1]]
            syntax.layer(
              parse_layer(grant=entries['grant'], revoke=entries['revoke'], limit=entries['limit'])
            )
          elif word in ('--pass', '--cred'):
            syntax.layer(parse_layer(grant=['bro.cred=' + words[index + 1]]))


def test_cheatsheet_shell_examples_parse_without_running_commands(monkeypatch):
  import importlib
  import re
  import shlex

  from bro import reference
  from bro.base.args import Parser

  class Captured(BaseException):
    def __init__(self, parser):
      self.parser = parser

  def capture(parser, argv):
    raise Captured(parser)

  commands = {
    'ride': 'ride.cli',
    'summon': 'bro.summon',
    'webview': 'bro.webview.cli',
    'benchmark-run': 'bro.bench.run',
  }
  text = (reference.DIRECTORY / 'permissions_cheatsheet.md').read_text()
  for body in re.findall(r'```sh\n(.*?)```', text, re.DOTALL):
    for line in body.splitlines():
      words = shlex.split(line)
      module = importlib.import_module(commands[words[0]])
      with monkeypatch.context() as context:
        context.setattr(Parser, 'parse', capture)
        with pytest.raises(Captured) as captured:
          module.main(words)
      captured.value.parser.parse(words)


def grown(installation, count):
  """The installation with `count` more targets, harnesses, and providers, all configured alike."""
  native = installation.harnesses['native']
  return replace(
    installation,
    bros={
      **installation.bros,
      **{
        f'extra{index}': type(f'Extra{index}', (Other,), {'name': f'extra{index}'})(
          credential_presence=installation.credential_kinds
        )
        for index in range(count)
      },
    },
    harnesses={
      **installation.harnesses,
      **{f'native{index}': native for index in range(count)},
    },
    provider_needs={
      **installation.provider_needs,
      **{f'echo{index}': frozenset({'provider_key'}) for index in range(count)},
    },
  )


def test_configured_authority_agrees_with_configured_kinds_on_every_start(installation):
  installation = grown(installation, 2)
  source = reading(
    installation,
    {'projects': {'/project': {'bros': {'child': {'creds': ['active+child-secret']}}}}},
  )
  authority = Authority(configured_entries(source))
  types = tuple(installation.declarations)
  for target, harness, provider in product(
    installation.bros, installation.harnesses, installation.provider_needs
  ):
    start = Descriptor('bro', {'target': target, 'harness': harness, 'provider': provider})
    held = configured_kinds(target, harness, provider, source)
    for kind in installation.credential_kinds:
      for instance in {source.picks(name).get(kind, '') for name in installation.bros}:
        atom = Atom('bro', f'cred.{kind}', instance)
        expected = kind in held and source.picks(target).get(kind, '') == instance
        assert authority.evaluate(atom, start, types).use.matches(start) == expected
        assert evaluate(authority, atom, start, start, give=False) == expected


def test_growing_an_axis_or_value_set_does_not_grow_entries_or_rendered_size(installation):
  sizes = {}
  for count in (0, 4):
    source = reading(grown(installation, count))
    mission = source.rules.start(Request('bro', {'target': 'child'}), Authority.top(), source)
    view = mission_view(mission)
    sizes[count] = (
      len(configured_entries(source)),
      len(json.dumps(view['permits'])),
      len(json.dumps(view['authority'])),
    )
  assert sizes[0] == sizes[4]


def test_later_starts_under_a_reading_do_no_per_target_work(installation, monkeypatch):
  source = reading(grown(installation, 6))
  start = Request('bro', {'target': 'child'})
  source.rules.start(start, Authority.top(), source)
  calls = []
  picks = Reading.picks
  monkeypatch.setattr(
    Reading, 'picks', lambda self, target: calls.append(target) or picks(self, target)
  )
  source.rules.start(start, Authority.top(), source)
  assert len(calls) <= len(installation.credential_kinds)


@pytest.mark.parametrize(
  ('selected', 'project', 'host', 'expected'),
  [
    ('auth', 'native', 'native', 'auth'),
    (None, 'auth', 'native', 'auth'),
    (None, None, 'native', 'native'),
  ],
  ids=['launch', 'project', 'host'],
)
def test_summon_harness_precedence(installation, selected, project, host, expected):
  source = Reading(
    Configuration.parse({'summon-harness': host}),
    {},
    installation,
    settings=HomeSettings(summon_harness=project),
  )
  descriptor = normalized(source, **({'harness': selected} if selected is not None else {}))
  assert descriptor.values['harness'] == expected


def test_unset_harness_does_not_prevent_reading_or_explicit_summons(installation):
  source = Reading(Configuration.parse({}), {}, installation)
  assert source.harness is None and source.summon_harness is None
  assert normalized(source, harness='native').values['harness'] == 'native'
  with pytest.raises(ValueError) as failure:
    normalized(source)
  assert (
    str(failure.value)
    == 'no harness: pass --harness, set home settings summon-harness, or set "summon-harness" in ~/.bro.json'
  )


def test_summon_does_not_use_the_root_harness_key(installation):
  source = Reading(
    Configuration.parse({'harness': 'native'}),
    {},
    installation,
    settings=HomeSettings(harness='auth'),
  )
  with pytest.raises(ValueError, match='summon-harness'):
    normalized(source)


@pytest.fixture
def absent_recipe_installation(installation):
  native = installation.harnesses['native']
  return replace(
    installation,
    harnesses={
      **installation.harnesses,
      'native': replace(native, recipes={'child': None, 'other': None}),
    },
  )


def test_absent_recipe_reading_has_no_llm_key_but_start_is_refused(absent_recipe_installation):
  from ride.authority_scope import checked_recipe

  source = reading(absent_recipe_installation)
  request = Request('bro', {'target': 'child'})
  descriptor = source.rules.normalize(request, source)
  assert 'provider' not in descriptor.values
  assert 'cred.provider_key' not in descriptor.values
  for check in (
    lambda: existence(descriptor, source, frozenset(), check_presence=False),
    lambda: checked_recipe(request, descriptor, source),
  ):
    with pytest.raises(
      ValueError,
      match='child declares no LLM recipe: pass --llm provider:model, or set "llm" under projects.<repo>.bros.child in ~/.bro.json',
    ):
      check()


@pytest.mark.parametrize('supplied', ['wire', 'host', 'default'])
def test_managed_recipe_supplied_to_a_persona_without_one(absent_recipe_installation, supplied):
  from ride.authority_scope import checked_recipe
  from ride.bro_worker import BroType

  recipes = ['echo:chosen+passthrough'] if supplied == 'host' else ['default']
  source = reading(
    absent_recipe_installation, {'projects': {'/project': {'bros': {'child': {'llm': recipes}}}}}
  )
  fields = {'target': 'child'}
  if supplied == 'wire':
    fields['llm'] = 'echo:chosen+passthrough'
  requested = Request('bro', BroType.option_values(fields, source).values)
  descriptor = source.rules.normalize(requested, source)
  if supplied == 'default':
    assert 'provider' not in descriptor.values
    with pytest.raises(ValueError, match='declares no LLM recipe'):
      checked_recipe(requested, descriptor, source)
  else:
    assert checked_recipe(requested, descriptor, source) == Recipe(model='chosen', passthrough=True)
    assert 'cred.provider_key' in descriptor.values


@pytest.mark.parametrize('value', [':echo', '::high', '+fast'])
def test_incomplete_summon_recipe_requires_a_base(absent_recipe_installation, value):
  from ride.bro_worker import BroType

  source = reading(absent_recipe_installation)
  with pytest.raises(ValueError, match='child declares no LLM recipe'):
    requested = Request(
      'bro', BroType.option_values({'target': 'child', 'llm': value}, source).values
    )
    source.rules.normalize(requested, source)


def test_unknown_host_provider_is_skipped_and_refused_when_exhausted(installation, monkeypatch):
  from bro.base import log

  messages = []
  monkeypatch.setattr(log, 'verbose', lambda message, *args: messages.append(message % args))
  config = {
    'projects': {
      '/project': {'bros': {'child': {'llm': ['uninstalled:remote', 'echo:local+passthrough']}}}
    }
  }
  source = reading(installation, config)
  assert normalized(source).values['model'] == 'local'
  assert any('uninstalled' in message and 'skipping' in message for message in messages)
  config['projects']['/project']['bros']['child']['llm'] = ['uninstalled:remote']
  source = reading(installation, config)
  with pytest.raises(ValueError, match='uninstalled.*install the plug-in'):
    normalized(source)
  assert (
    normalized(source, provider='echo', model='override', passthrough=True).values['model']
    == 'override'
  )


def test_recipe_layers_preserve_persona_knobs_but_named_provider_resets_them(installation):
  from bro.openai.declaration import MODELS, Recipe as OpenAISpec
  from ride.authority_scope import checked_recipe
  from ride.bro_worker import BroType

  standing = OpenAISpec(reasoning_effort='high', compact_threshold=12345)
  installed = recipe_installation(installation, 'native', standing)
  source = reading(installed, {'projects': {'/project': {'bros': {'child': {'llm': [':terra']}}}}})
  requested = Request('bro', {'target': 'child', 'effort': 'low'})
  descriptor = source.rules.normalize(requested, source)
  assert checked_recipe(requested, descriptor, source) == replace(
    standing, model=MODELS['terra'], reasoning_effort='low'
  )
  requested = Request(
    'bro', BroType.option_values({'target': 'child', 'llm': 'openai:sol'}, source).values
  )
  descriptor = source.rules.normalize(requested, source)
  assert checked_recipe(requested, descriptor, source) == OpenAISpec(model=MODELS['sol'])


def test_summon_preset_precedence_and_provider_no_shadowing(installation):
  from ride.bro_worker import BroType

  source = reading(
    installation,
    {'llm': {'chosen': 'echo:host+passthrough', 'echo': 'uninstalled:model'}},
    project={'llm': {'chosen': 'echo:project+passthrough'}},
  )
  selected = BroType.option_values({'target': 'child', 'llm': 'chosen'}, source).values
  assert selected['model'] == 'host'
  selected = BroType.option_values({'target': 'child', 'llm': 'echo'}, source).values
  assert selected['provider'] == 'echo'
  assert 'model' not in selected


def test_configuration_parses_uninstalled_recipes():
  configuration = Configuration.parse(
    {
      'llm': {'future': 'uninstalled:model'},
      'projects': {'/elsewhere': {'bros': {'missing': {'llm': ['uninstalled:model']}}}},
    }
  )
  assert configuration.projects['/elsewhere'].bros['missing'].recipes == ('uninstalled:model',)


def test_default_without_a_declaration_does_not_hide_missing_host_plugins(
  absent_recipe_installation,
):
  source = reading(
    absent_recipe_installation,
    {'projects': {'/project': {'bros': {'child': {'llm': ['uninstalled:model', 'default']}}}}},
  )
  with pytest.raises(ValueError, match='uninstalled.*install the plug-in'):
    normalized(source)


@pytest.mark.parametrize('surface', ['authority', 'scope'])
@pytest.mark.parametrize(
  'selection',
  [
    {'llm': 'openai:gpt-7:high'},
    {'llm': 'openai:gpt-7:high+passthrough'},
    {'provider': 'openai', 'model': 'gpt-7', 'effort': 'high', 'passthrough': True},
  ],
)
def test_admission_on_the_final_launch_recipe(
  installation, monkeypatch, capsys, surface, selection
):
  from bro.openai.declaration import Recipe as OpenAISpec
  from ride.authority_launch import request
  from ride.authority_scope import checked_recipe
  from ride.scope_report import report_scope

  source = reading(recipe_installation(installation, 'native', OpenAISpec()))
  if surface == 'scope':
    monkeypatch.setattr(Reading, 'take', lambda *_: source)
    options = {name: value for name, value in selection.items() if name != 'llm'}
    status = report_scope(None, 'child', 'native', selection.get('llm'), recipe_options=options)
    assert status == (1 if selection == {'llm': 'openai:gpt-7:high'} else 0)
    output = capsys.readouterr().out
    assert 'gpt-7' in output
    assert 'passthrough' in output
    return

  def launch():
    requested, _ = request(
      BroType, {'target': 'child', **selection}, source, source.operator(), forward=False
    )
    mission = source.rules.start(requested, source.operator(), source)
    return checked_recipe(mission.request, mission.descriptor, source)

  if selection == {'llm': 'openai:gpt-7:high'}:
    with pytest.raises(ValueError, match='gpt-7.*\\+passthrough'):
      launch()
  else:
    recipe = launch()
    assert recipe.selection()['model'] == 'gpt-7' and recipe.passthrough


@pytest.mark.parametrize(
  'selection,model,passthrough',
  [
    ({}, None, False),
    ({'llm': ':sol'}, 'gpt-6.1-sol', False),
    ({'llm': '+passthrough'}, 'gpt-7', True),
  ],
)
def test_unlisted_standing_recipe_is_overridable(installation, selection, model, passthrough):
  from bro.openai.declaration import Recipe as OpenAISpec
  from ride.authority_scope import checked_recipe

  installed = recipe_installation(installation, 'native', OpenAISpec(model='gpt-7'))
  source = reading(installed)
  fields = {'target': 'child', **selection}
  if model is None:
    with pytest.raises(ValueError, match='gpt-7.*passthrough'):
      BroType.option_values(fields, source)
      normalized(source)
  else:
    values = BroType.option_values(fields, source).values
    requested = Request('bro', values)
    descriptor = source.rules.normalize(requested, source)
    spec = checked_recipe(requested, descriptor, source)
    assert spec.selection()['model'] == model and spec.passthrough is passthrough
  source = reading(
    installed, {'projects': {'/project': {'bros': {'child': {'llm': ['openai:terra']}}}}}
  )
  assert normalized(source).values['model'] == 'gpt-5.6-terra'


@pytest.mark.parametrize(
  'text,admitted',
  [
    ('::xhigh', True),
    ('anthropic', True),
    ('anthropic:claude-opus-6', False),
    ('anthropic:claude-opus-6+passthrough', True),
  ],
)
def test_claude_deferred_listed_and_unlisted_admission(installation, text, admitted):
  from bro.anthropic.declaration import Recipe as AnthropicSpec
  from ride.authority_scope import checked_recipe

  source = reading(
    recipe_installation(installation, 'claude', AnthropicSpec(model=None, effort=None)),
    settings=HomeSettings(summon_harness='claude'),
  )

  def launch():
    values = BroType.option_values({'target': 'child', 'llm': text}, source).values
    requested = Request('bro', values)
    return checked_recipe(requested, source.rules.normalize(requested, source), source)

  if admitted:
    assert launch().admitted()
  else:
    with pytest.raises(ValueError, match='claude-opus-6.*passthrough'):
      launch()


def test_passthrough_cannot_be_overridden_or_set_on_a_manual_start(installation):
  source = reading(installation)
  from bro.authority.rules import Override

  requested = Request('bro', {'target': 'child', 'passthrough': True})
  descriptor = source.rules.normalize(requested, source)
  source.rules.use(source.operator(), descriptor)
  mission = source.rules.start(requested, source.operator(), source)
  with pytest.raises(Refusal, match='not overridable'):
    source.rules.override(mission, Override(values={'passthrough': True}), source.operator())
  with pytest.raises(ValueError, match='launch-owned.*passthrough'):
    BroType.option_values({'target': 'child', 'manual': True, 'passthrough': True}, source)
  with pytest.raises(ValueError, match='passthrough.*flag'):
    BroType.option_values({'target': 'child', 'passthrough': 1}, source)


def test_the_bro_installation_reads_its_shipped_defaults(installation_environment, installation):
  from bro.workspace.installation import MARKER
  from bro.workspace.settings import home_settings

  (installation_environment / MARKER).write_text('bro\n')
  settings = home_settings('bro')
  source = Reading.take(installation=installation)
  assert source.resolve_harness() == settings.harness
  assert source.default_bro == settings.default_bro
