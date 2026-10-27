import json
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from bro.authority.authority import Atom
from bro.authority.patterns import Descriptor
from bro.authority.syntax import parse_layer
from bro.base import credentials, host_config
from bro.base.condition import when
from bro.bro import BaseBro, feature
from bro.harness import Harness
from bro.mcp import MCPServerSpec, ToolLayer, creds
from bro.native.echo_declaration import LLMSpec
from bro.worker_types import installed_types
from ride import scope
from ride.authority_reading import Configuration, HarnessReading, Installation, Policy, Reading
from ride.workspace.store import ScopedSecrets


class ScopedBro(BaseBro):
  name = 'scoped'
  extra_secrets = ('github',)
  may_summon = ('reviewer',)
  may_launch = ('webview',)


class GatedBro(BaseBro):
  name = 'gated'
  features: ClassVar = {'payload': creds.contains('gate')}
  tools: ClassVar = [
    when(
      feature('payload'),
      ToolLayer(
        server_specs=(
          MCPServerSpec(
            'payload',
            build=lambda: pytest.fail('manifest built a server'),
            needed_secrets=('payload',),
          ),
        )
      ),
    )
  ]


@pytest.fixture
def scoped_installation(monkeypatch, tmp_path):
  kinds = frozenset({'github', 'cookies', 'auth', 'trails', 'provider_key', 'gate', 'payload'})
  registry = {kind: credentials.CredentialKind(kind, kind) for kind in kinds}
  monkeypatch.setattr(credentials, 'default_registry', lambda: registry)
  store = tmp_path / 'store'
  material = store / credentials.MATERIAL_DIR
  material.mkdir(parents=True)
  for name in ('github', 'github+reviewer', 'auth', 'provider_key'):
    (material / f'{name}.cred').write_text(name)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
  monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(tmp_path / 'host.json'))
  harness = Harness('test')
  harness.llm_providers = ('echo',)
  installation = Installation(
    {
      bro.name: bro(credential_presence=kinds, credential_kinds=kinds)
      for bro in (ScopedBro, GatedBro)
    },
    {
      'test': HarnessReading(
        harness, {'scoped': LLMSpec(), 'gated': LLMSpec()}, auth_kind='auth', llm_key=True
      )
    },
    kinds,
    {'echo': frozenset({'provider_key'})},
    {name: worker.authority_declaration(kinds) for name, worker in installed_types().items()},
  )
  monkeypatch.setattr(Installation, 'take', lambda: installation)
  return installation


def _recipe(installation):
  return scope.ScopeRecipe('test', installation.harnesses['test'].harness, 'auth', True)


def _reading(installation, configuration=None, project=None):
  return Reading(
    Configuration.parse(configuration or {}),
    {},
    installation,
    project={'summon-harness': 'test'},
    project_policy=project or Policy(),
  )


def _pair(reading, atom):
  descriptor = Descriptor('bro', {'target': 'scoped'})
  authority = reading.installation.declarations['bro'].base(descriptor, reading)
  return authority.evaluate(atom, descriptor, reading.installation.declarations)


class TestScopedSecrets:
  def test_manifest_includes_harness_auth_and_provider_needs(self, scoped_installation):
    result = scope.scoped_secrets('scoped', _recipe(scoped_installation))
    assert result.required == {'github', 'auth', 'provider_key'}
    assert 'trails' not in result.optional

  def test_pick_holds_a_kind_and_selects_its_instance(self, scoped_installation):
    result = scope.scoped_secrets('scoped', _recipe(scoped_installation), cred=['github+reviewer'])
    assert result.selection['github'] == 'reviewer'
    assert 'github' in result.required

  def test_store_defaults_are_read_under_the_selected_instance(self, scoped_installation):
    (Path(credentials.STORE_DIR) / credentials.STORE_FILE).write_text(
      json.dumps({'defaults': ['github+reviewer']})
    )
    result = scope.scoped_secrets('scoped', _recipe(scoped_installation))
    assert scope.credential_store(result).get('github') == 'github+reviewer'

  def test_missing_required_instance_is_a_scope_error(self, scoped_installation):
    with pytest.raises(scope.LaunchScopeError, match='required credential instances.*github'):
      scope.scoped_secrets('scoped', _recipe(scoped_installation), cred=['github+absent'])

  def test_explicit_pick_conflicting_with_drop_is_refused(self, scoped_installation):
    with pytest.raises(scope.LaunchScopeError, match='both set and dropped'):
      scope.scoped_secrets(
        'scoped', _recipe(scoped_installation), cred=['github+reviewer'], revoke=['github']
      )

  def test_operator_caps_apply_to_the_root_request(self, scoped_installation):
    Path(host_config.HOST_CONFIG_FILE).write_text(json.dumps({'operator': {'revoke': ['@scoped']}}))
    with pytest.raises(scope.LaunchScopeError, match='use rule.*scoped'):
      scope.scoped_secrets('scoped', _recipe(scoped_installation))

  def test_malformed_permission_is_a_scope_error(self, scoped_installation):
    with pytest.raises(scope.LaunchScopeError, match='invalid name'):
      scope.scoped_secrets('scoped', _recipe(scoped_installation), grant=[':launch.bro'])

  def test_a_revoked_optional_kind_is_not_promoted_to_required(self, scoped_installation):
    result = scope.scoped_secrets('scoped', _recipe(scoped_installation), revoke=['trails'])
    assert 'trails' not in result.selection
    assert 'trails' not in result.required | result.optional

  def test_disabled_recording_does_not_hydrate_the_recorder_kind(self, scoped_installation):
    (Path(credentials.STORE_DIR) / credentials.MATERIAL_DIR / 'trails.cred').write_text('recorder')
    enabled = scope.scoped_secrets('scoped', _recipe(scoped_installation))
    disabled = scope.scoped_secrets('scoped', _recipe(scoped_installation), recording=False)
    assert 'trails' in enabled.optional
    assert 'trails' not in disabled.required | disabled.optional

  def test_credential_presence_selects_components_without_loading_material(
    self, scoped_installation, monkeypatch
  ):
    material = Path(credentials.STORE_DIR) / credentials.MATERIAL_DIR
    (material / 'gate+reviewer.cred').write_bytes(b'\xff')
    (material / 'payload.cred').write_text('payload')
    monkeypatch.setattr(credentials.LocalSource, 'fetch', lambda *_: pytest.fail('material read'))
    result = scope.scoped_secrets('gated', _recipe(scoped_installation), cred=['gate+reviewer'])
    assert 'payload' in result.required
    assert result.selection['gate'] == 'reviewer'

  def test_an_absent_gate_does_not_hydrate_its_component(self, scoped_installation):
    result = scope.scoped_secrets('gated', _recipe(scoped_installation))
    assert 'payload' in result.selection
    assert 'payload' not in result.required | result.optional

  def test_a_missing_selected_components_requirement_is_named(self, scoped_installation):
    material = Path(credentials.STORE_DIR) / credentials.MATERIAL_DIR
    (material / 'gate+reviewer.cred').write_text('gate')
    with pytest.raises(scope.LaunchScopeError, match='missing:.*payload'):
      scope.scoped_secrets('gated', _recipe(scoped_installation), cred=['gate+reviewer'])


class TestAuthorityLayers:
  def test_declaration_seeds_type_and_target_permissions(self, scoped_installation):
    reading = _reading(scoped_installation)
    assert not _pair(reading, Atom('webview')).use.is_empty
    assert not _pair(reading, Atom('bro', 'target', 'reviewer')).use.is_empty

  def test_caps_precede_grants_in_each_layer(self, scoped_installation):
    reading = _reading(
      scoped_installation,
      {'defaults': {'grant': ['webview']}},
      Policy(parse_layer(revoke=['webview'])),
    )
    assert not _pair(reading, Atom('webview')).use.is_empty
    reading = _reading(scoped_installation, {'defaults': {'revoke': ['webview']}})
    assert _pair(reading, Atom('webview')).use.is_empty

  def test_limit_restricts_usable_starts(self, scoped_installation):
    reading = _reading(
      scoped_installation,
      {
        'defaults': {'limit': ['@reviewer for bro{placement=boxed}']},
      },
    )
    pair = _pair(reading, Atom('bro', 'target', 'reviewer'))
    assert pair.use.matches(Descriptor('bro', {'placement': 'boxed'}))
    assert not pair.use.matches(Descriptor('bro', {'placement': 'unboxed'}))
    assert pair.give.is_empty

  def test_give_only_instance_needs_neither_start_permission_nor_material(
    self, scoped_installation
  ):
    reading = _reading(
      scoped_installation,
      {
        'defaults': {
          'revoke': ['webview'],
          'grant': ['webview.cred=cookies+absent to @reviewer'],
        }
      },
    )
    assert _pair(reading, Atom('webview')).use.is_empty
    pair = _pair(reading, Atom('webview', 'cred.cookies', 'absent'))
    assert pair.use.is_empty
    assert pair.give.matches(Descriptor('bro', {'target': 'reviewer', 'placement': 'boxed'}))
    assert not pair.give.matches(Descriptor('bro', {'target': 'reviewer', 'placement': 'join'}))

  def test_unknown_names_fail_only_when_their_layer_applies(self, scoped_installation):
    reading = _reading(scoped_installation, {'projects': {'/other': {'grant': ['unknown']}}})
    assert not _pair(reading, Atom('bro')).use.is_empty
    reading = _reading(scoped_installation, {'defaults': {'grant': ['unknown']}})
    with pytest.raises(ValueError, match='unknown mission type'):
      _pair(reading, Atom('bro'))

  def test_overlapping_grant_and_revoke_are_refused_on_resolution(self, scoped_installation):
    reading = _reading(
      scoped_installation,
      {
        'defaults': {
          'grant': ['bro.placement=*'],
          'revoke': ['bro.placement=join'],
        }
      },
    )
    with pytest.raises(ValueError, match='cannot grant and revoke'):
      _pair(reading, Atom('bro'))


class TestLaunchViewStore:
  def test_view_is_bounded_by_the_credential_manifest(self):
    with patch('ride.scope.credentials.scoped_view_store', return_value='view') as view:
      assert (
        scope.launch_view_store(ScopedSecrets({'github'}, {'trails'}, {'github': 'reviewer'}))
        == 'view'
      )
    assert view.call_args.args[0].selection == {'github': 'reviewer'}
    assert view.call_args.args[1] == {'github'}
    assert view.call_args.kwargs == {'optional': {'trails'}}

  def test_missing_secret_becomes_a_scope_error(self):
    with patch(
      'ride.scope.credentials.scoped_view_store', side_effect=credentials.SecretNotFound('github')
    ):
      with pytest.raises(scope.LaunchScopeError, match='github'):
        scope.launch_view_store(ScopedSecrets({'github'}, set()))
