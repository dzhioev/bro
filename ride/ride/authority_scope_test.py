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
from bro.llm.llm import LLMSpec as Recipe
from bro.llm.llms.echo import LLMSpec
from bro.mcp import MCPServerSpec, ToolLayer, creds
from bro.worker_types import installed_types
from ride.authority_launch import mission_view
from ride.authority_reading import Configuration, HarnessReading, Installation, Reading
from ride.authority_scope import configured_entries, configured_kinds, existence
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
  native.llm_providers = ('echo',)
  auth = Harness('auth')
  auth.llm_providers = ('echo',)
  return Installation(
    {'child': Child(credential_presence=kinds), 'other': Other(credential_presence=kinds)},
    {
      'native': HarnessReading(native, {'child': LLMSpec(), 'other': LLMSpec()}, llm_key=True),
      'auth': HarnessReading(auth, {'child': LLMSpec(), 'other': LLMSpec()}, auth_kind='auth'),
    },
    kinds,
    {'echo': frozenset({'provider_key'})},
    {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
  )


def reading(installation, configuration=None, project=None, defaults=None):
  return Reading(
    Configuration.parse(configuration or {}),
    defaults or {},
    installation,
    host_config.Attachment(path='/project', url='https://github.com/example/project.git'),
    project or {'summon-harness': 'native'},
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


def test_reading_snapshots_whole_config_defaults_and_working_tree(tmp_path, installation):
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
  host.write_text('{broken')
  (store / 'creds.json').write_text('{broken')
  (project / 'pyproject.toml').write_text('[broken')
  assert source.picks('child')['cookies'] == 'first'
  assert source.configuration.raw['user']['creds'] == ('cookies+operator',)
  assert source.configuration.raw['llm']['local'] == 'echo:echo'
  assert source.summon_depth == 7
  assert source.project_policy.layer.grant
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
    source, project_policy=replace(source.project_policy, picks={'consumer_only': 'host'})
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


def test_repository_refuses_instance_entries(tmp_path, installation):
  project = tmp_path / 'project'
  project.mkdir()
  for entry in ('bro.cred=cookies+profile', 'bro.cred=cookies+* to @child'):
    (project / 'pyproject.toml').write_text('[tool.bro]\ngrant = [' + json.dumps(entry) + ']')
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
        '/project': {'bros': {'child': {'llm': ['echo:custom']}}},
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


def test_url_attachment_reads_resolved_commit_not_working_tree(tmp_path, installation):
  subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
  project = tmp_path / 'pyproject.toml'
  project.write_text('[tool.bro]\nsummon-harness = "native"\ngrant = ["@other"]')
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
  project.write_text('[tool.bro]\nsummon-harness = "auth"\nrevoke = ["@other"]')
  source = Reading.take(
    Repository('https://github.com/example/project.git', tmp_path, commit),
    host=tmp_path / 'absent',
    store=tmp_path / 'absent',
    installation=installation,
  )
  assert source.summon_harness == 'native'
  assert source.project_policy.layer.grant and not source.project_policy.layer.revoke


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
  from bro.llm.llms.openai import LLMSpec as OpenAISpec

  native = Harness('native')
  native.llm_providers = ('openai',)
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


def recipe_installation(installation: Installation, harness: str, spec: Recipe) -> Installation:
  driver = Harness(harness)
  driver.llm_providers = (spec.TYPE,)
  return replace(
    installation,
    harnesses={harness: HarnessReading(driver, dict.fromkeys(installation.bros, spec))},
    provider_needs={spec.TYPE: frozenset()},
  )


def test_deferred_recipe_choices_are_checked_then_omitted(installation):
  from bro.llm.llms.anthropic import LLMSpec as AnthropicSpec
  from ride.authority_scope import DEFAULT, checked_recipe

  spec = AnthropicSpec(model=None, effort=None)
  source = reading(
    recipe_installation(installation, 'claude', spec), project={'summon-harness': 'claude'}
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
    {'target': 'child', 'provider': 'anthropic', 'model': 'claude-opaque-new', 'effort': 'high'},
  )
  explicit_descriptor = source.rules.normalize(explicit, source)
  cap = source.syntax.layer(parse_layer(revoke=['bro.model=~default', 'bro.effort=~default'])).cap
  source.rules.use(Authority.top().meet(cap), explicit_descriptor)


def test_opaque_literal_model_ids_do_not_collide_with_default_state(installation):
  from bro.llm.llms.openai import LLMSpec as OpenAISpec
  from ride.authority_scope import DEFAULT, checked_recipe

  source = reading(recipe_installation(installation, 'native', OpenAISpec()))
  for model in ('default', '~default', 'vendor-private-variant'):
    request = Request('bro', {'target': 'child', 'provider': 'openai', 'model': model})
    descriptor = source.rules.normalize(request, source)
    assert descriptor.values['model'] == model and descriptor.values['model'] != DEFAULT
    source.rules.use(Authority.top(), descriptor)
    assert checked_recipe(request, descriptor, source).selection()['model'] == model
  with pytest.raises(ValueError, match='cannot defer model'):
    normalized(source, provider='openai', model=DEFAULT)


@pytest.mark.parametrize('effort', ['none', 'minimal', None])
def test_provider_efforts_reconstruct_exactly_and_remain_governed(installation, effort):
  from bro.authority.syntax import render_values
  from bro.llm.llms.openai import LLMSpec as OpenAISpec
  from ride.authority_scope import checked_recipe

  standing = OpenAISpec(
    model='vendor-private-variant',
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
    {'target': 'child', 'provider': 'openai', 'model': 'vendor-private-variant', 'effort': value},
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
    project={'summon-harness': project} if project is not None else {},
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
    == 'no harness: pass --harness, set [tool.bro] summon-harness, or set "summon-harness" in ~/.bro.json'
  )


def test_summon_does_not_use_the_root_harness_key(installation):
  source = Reading(
    Configuration.parse({'harness': 'native'}), {}, installation, project={'harness': 'auth'}
  )
  with pytest.raises(ValueError, match='summon-harness'):
    normalized(source)
