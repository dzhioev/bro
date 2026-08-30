import importlib.metadata
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.base import credentials, host_config
from bro.base.args import Parser


def _registry(*names: str) -> dict[str, credentials.CredentialKind]:
  return {name: credentials.CredentialKind(name, f'{name} credential') for name in names}


def _write_material(store_dir: Path, name: str, value: str) -> Path:
  path = store_dir / credentials.MATERIAL_DIR / f'{name}{credentials.MATERIAL_SUFFIX}'
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(value)
  return path


def _write_sources(store_dir: Path, data: object) -> None:
  store_dir.mkdir(parents=True, exist_ok=True)
  (store_dir / credentials.SOURCES_FILE).write_text(json.dumps(data))


def _store(
  store_dir: Path,
  *names: str,
  selection: dict[str, str] | None = None,
) -> credentials.Store:
  return credentials.Store(_registry(*names), store_dir, selection or {})


class _TicketSource(credentials.MintingSource):
  TYPE = 'ticket'

  def __init__(self, prefix: str):
    if not isinstance(prefix, str):
      raise ValueError('ticket prefix must be a string')
    super().__init__(prefix=prefix)
    self.prefix = prefix

  def mint(self, config: dict) -> credentials.Minted:
    return credentials.Minted(
      f'{self.prefix}:{config["seed"]}', datetime.now(UTC) + timedelta(hours=1)
    )


@pytest.fixture
def ticket_source(monkeypatch):
  entry_point = importlib.metadata.EntryPoint(
    'ticket', 'bro.base.credentials_test:_TicketSource', credentials._CREDENTIAL_SOURCE_GROUP
  )
  original = credentials._entry_points
  monkeypatch.setattr(
    credentials,
    '_entry_points',
    lambda group: (
      (entry_point,) if group == credentials._CREDENTIAL_SOURCE_GROUP else original(group)
    ),
  )


class TestRegistry:
  def test_builtin_and_contributed_entries_have_code_only_shape(self):
    registry = credentials.default_registry()

    assert {'github', 'openai', 'harbor'} <= set(registry)
    assert all(entry.description for entry in registry.values())
    assert registry['github'].install is not None

  @pytest.mark.parametrize('field', ['sources', 'instance', 'filename'])
  def test_retired_or_unknown_field_names_valid_storage_locations(self, field: str):
    with pytest.raises(ValueError, match='source configuration belongs in creds.json'):
      credentials.CredentialKind.from_dict(
        'github', {'description': 'GitHub access', field: object()}
      )

  def test_description_is_required_and_one_line(self):
    with pytest.raises(ValueError, match='missing required description'):
      credentials.CredentialKind.from_dict('github', {})
    with pytest.raises(ValueError, match='one trimmed line'):
      credentials.CredentialKind.from_dict('github', {'description': 'two\nlines'})

  def test_contribution_cannot_override_a_builtin(self, monkeypatch):
    monkeypatch.setattr(
      credentials,
      '_contributed_registry_data',
      lambda: {'github': {'description': 'other'}},
    )
    with pytest.raises(ValueError, match="duplicates kind 'github'"):
      credentials.default_registry()

  def test_install_template_is_validated_at_registry_load(self):
    with pytest.raises(ValueError, match='unknown section'):
      credentials.CredentialKind('github', 'GitHub access', install={'unknown': {'x': 'y'}})


class TestStore:
  def test_local_material_uses_the_convention_path(self, tmp_path: Path):
    _write_material(tmp_path, 'openai', '  token  ')
    store = _store(tmp_path, 'openai')

    assert store.get('openai') == 'token'
    assert isinstance(store.winning_source('openai'), credentials.LocalSource)

  def test_kind_read_applies_selection_and_instance_read_is_exact(self, tmp_path: Path):
    _write_material(tmp_path, 'github', 'bare')
    _write_material(tmp_path, 'github+reviewer', 'selected')
    store = _store(tmp_path, 'github', selection={'github': 'reviewer'})

    assert store.get('github') == 'selected'
    assert store.get_instance('github') == 'bare'
    assert store.get_instance('github+reviewer') == 'selected'

  def test_the_empty_instance_is_selectable_like_any_other(self, tmp_path: Path):
    _write_material(tmp_path, 'github', 'default')
    _write_material(tmp_path, 'github+bot', 'bot')
    store = _store(tmp_path, 'github', selection={'github': ''})

    assert store.get('github') == 'default'
    assert store.get_instance('github+') == 'default'

  def test_material_spelling_the_empty_instance_long_fails_at_construction(self, tmp_path: Path):
    _write_material(tmp_path, 'github+', 'material')

    with pytest.raises(ValueError, match="'github\\+', which spells the stored name 'github'"):
      _store(tmp_path, 'github')

  def test_annotation_spelling_the_empty_instance_long_fails_at_construction(self, tmp_path: Path):
    _write_sources(tmp_path, {'github+': {'type': 'ssm', 'parameter': '/p'}})

    with pytest.raises(ValueError, match='which spells the stored name'):
      _store(tmp_path, 'github')

  def test_invalid_selection_fails_at_construction(self, tmp_path: Path):
    with pytest.raises(ValueError, match='unknown credential kind'):
      credentials.Store(_registry('github'), tmp_path, {'brog': 'github'})
    with pytest.raises(ValueError, match='storage instance'):
      credentials.Store(_registry('github'), tmp_path, {'github+bot': 'x'})

  def test_missing_selected_instance_names_the_storage_name(self, tmp_path: Path):
    store = _store(tmp_path, 'github', selection={'github': 'reviewer'})

    with pytest.raises(credentials.SecretNotFound) as error:
      store.get('github')
    assert error.value.name == 'github+reviewer'

  def test_known_names_are_registry_kinds_even_when_material_is_absent(self, tmp_path: Path):
    store = _store(tmp_path, 'github', 'openai')

    assert store.known_names() == frozenset({'github', 'openai'})
    assert not store.available('github')

  def test_instance_names_enumerate_material_and_typed_sources(self, tmp_path: Path):
    _write_material(tmp_path, 'github+reviewer', 'token')
    _write_material(tmp_path, 'unknown', 'ignored')
    _write_sources(
      tmp_path,
      {
        'openai': {'type': 'ssm', 'parameter': '/openai'},
        'other': {'type': 'ssm', 'parameter': '/other'},
      },
    )
    store = _store(tmp_path, 'github', 'openai')

    assert store.instance_names() == frozenset({'github+reviewer', 'openai'})

  def test_unknown_kind_annotation_is_skipped_for_shared_dotfiles(self, tmp_path: Path):
    _write_sources(tmp_path, {'consumer_only': {'type': 'from-another-package'}})

    assert _store(tmp_path, 'github').known_names() == frozenset({'github'})

  def test_known_source_shape_is_validated_even_for_an_unknown_kind(self, tmp_path: Path):
    _write_sources(tmp_path, {'consumer_only': {'type': 'ssm', 'unknown': True}})

    with pytest.raises(ValueError, match='unknown fields'):
      _store(tmp_path, 'github')

  @pytest.mark.parametrize(
    'data, message',
    [
      ([], 'must be a json object'),
      ({'github': []}, 'must be an object'),
      ({'github': {}}, 'non-empty string type'),
      ({'github': {'type': 'ssm'}}, "missing 'parameter'"),
      ({'github': {'type': 'ssm', 'parameter': '/x', 'extra': 1}}, 'unknown fields'),
    ],
  )
  def test_malformed_source_file_fails(self, tmp_path: Path, data: object, message: str):
    _write_sources(tmp_path, data)

    with pytest.raises(ValueError, match=message):
      _store(tmp_path, 'github')

  def test_ssm_source_takes_only_typed_parameters(self, tmp_path: Path, monkeypatch):
    class ParameterNotFound(Exception):
      pass

    class Client:
      exceptions = SimpleNamespace(ParameterNotFound=ParameterNotFound)

      def get_parameter(self, **arguments):
        assert arguments == {'Name': '/service/token', 'WithDecryption': True}
        return {'Parameter': {'Value': 'from-ssm'}}

    fake_boto3 = SimpleNamespace(client=lambda service, region_name=None: Client())
    monkeypatch.setitem(sys.modules, 'boto3', fake_boto3)
    _write_sources(
      tmp_path,
      {'github': {'type': 'ssm', 'parameter': '/service/token', 'region': 'eu-west-1'}},
    )

    assert _store(tmp_path, 'github').get('github') == 'from-ssm'

  def test_minting_source_receives_typed_parameters_and_material_path(
    self, tmp_path: Path, ticket_source
  ):
    _write_material(tmp_path, 'github', '{"seed": "abc"}')
    _write_sources(tmp_path, {'github': {'type': 'ticket', 'prefix': 'minted'}})

    assert _store(tmp_path, 'github').get('github') == 'minted:abc'

  def test_local_values_cache_for_store_lifetime(self, tmp_path: Path):
    path = _write_material(tmp_path, 'openai', 'first')
    store = _store(tmp_path, 'openai')

    assert store.get('openai') == 'first'
    path.write_text('second')
    assert store.get('openai') == 'first'

  def test_minting_values_are_not_cached_by_store(self, tmp_path: Path, ticket_source, monkeypatch):
    _write_material(tmp_path, 'github', '{"seed": "abc"}')
    _write_sources(tmp_path, {'github': {'type': 'ticket', 'prefix': 'minted'}})
    store = _store(tmp_path, 'github')
    source = store._sources['github']
    assert isinstance(source, _TicketSource)

    assert store.get('github') == 'minted:abc'
    source._minted = credentials.Minted('fresh', datetime.now(UTC) + timedelta(hours=1))
    assert store.get('github') == 'fresh'


class TestReferences:
  def test_kind_reference_uses_selection_and_instance_reference_is_exact(self, tmp_path: Path):
    _write_material(tmp_path, 'github', '{"login": "bare"}')
    _write_material(tmp_path, 'github+reviewer', '{"login": "reviewer"}')
    _write_material(
      tmp_path,
      'brog',
      json.dumps(
        {
          'selected': {'$cred': 'github', 'field': 'login'},
          'bare': {'$cred': 'github+reviewer', 'field': 'login'},
        }
      ),
    )
    store = _store(tmp_path, 'brog', 'github', selection={'github': 'reviewer'})

    assert store.get_json('brog') == {'selected': 'reviewer', 'bare': 'reviewer'}

  def test_reference_to_missing_value_propagates_absence(self, tmp_path: Path):
    _write_material(tmp_path, 'brog', '{"token": {"$cred": "github"}}')
    store = _store(tmp_path, 'brog', 'github')

    assert store.try_get('brog') is None
    with pytest.raises(credentials.SecretNotFound) as error:
      store.get('brog')
    assert error.value.name == 'github'

  def test_reference_field_and_nested_expansion(self, tmp_path: Path):
    _write_material(tmp_path, 'github', '{"token": "secret"}')
    _write_material(tmp_path, 'brog', '{"auth": {"$cred": "github", "field": "token"}}')

    assert _store(tmp_path, 'brog', 'github').get_json('brog') == {'auth': 'secret'}

  def test_reference_cycle_fails(self, tmp_path: Path):
    _write_material(tmp_path, 'brog', '{"$cred": "github"}')
    _write_material(tmp_path, 'github', '{"$cred": "brog"}')

    with pytest.raises(ValueError, match='credential reference cycle'):
      _store(tmp_path, 'brog', 'github').get('brog')

  def test_malformed_reference_fails(self, tmp_path: Path):
    _write_material(tmp_path, 'brog', '{"$cred": "github", "unexpected": true}')
    _write_material(tmp_path, 'github', 'token')

    with pytest.raises(ValueError, match='unknown keys'):
      _store(tmp_path, 'brog', 'github').get('brog')


class TestScopedStore:
  def test_hydrates_convention_paths_and_reports_declared_kinds(self, tmp_path: Path):
    _write_material(tmp_path, 'openai+benchmark', 'key')
    source = _store(tmp_path, 'openai', selection={'openai': 'benchmark'})

    files, kinds = credentials.build_scoped_store(source, {'openai'})

    assert files == {'creds/openai.cred': b'key', 'creds.json': b'{}'}
    assert kinds == frozenset({'openai'})

  def test_explicit_instance_materializes_under_its_kind(self, tmp_path: Path):
    _write_material(tmp_path, 'github+reviewer', 'token')

    files, kinds = credentials.build_scoped_store(_store(tmp_path, 'github'), {'github+reviewer'})

    assert files['creds/github.cred'] == b'token'
    assert kinds == frozenset({'github'})

  def test_optional_absence_is_skipped_and_required_absence_fails(self, tmp_path: Path):
    source = _store(tmp_path, 'openai')

    files, kinds = credentials.build_scoped_store(source, set(), optional={'openai'})
    assert files == {'creds.json': b'{}'}
    assert kinds == frozenset()
    with pytest.raises(credentials.SecretNotFound):
      credentials.build_scoped_store(source, {'openai'})

  def test_unknown_required_kind_fails_and_unknown_optional_kind_is_skipped(self, tmp_path: Path):
    source = _store(tmp_path, 'openai')

    with pytest.raises(ValueError, match='unknown secret'):
      credentials.build_scoped_store(source, {'typo'})
    files, _ = credentials.build_scoped_store(source, set(), optional={'typo'})
    assert files == {'creds.json': b'{}'}

  def test_two_instances_of_one_kind_fail_before_hydration(self, tmp_path: Path):
    source = _store(tmp_path, 'github')

    with pytest.raises(ValueError, match='instances of the same kind'):
      credentials.build_scoped_store(source, {'github', 'github+reviewer'})

  def test_minting_source_ships_config_and_typed_annotation(self, tmp_path: Path, ticket_source):
    _write_material(tmp_path, 'github', '{"seed": "abc"}')
    _write_sources(tmp_path, {'github': {'type': 'ticket', 'prefix': 'minted'}})

    files, _ = credentials.build_scoped_store(_store(tmp_path, 'github'), {'github'})

    assert files['creds/github.cred'] == b'{"seed": "abc"}'
    assert json.loads(files['creds.json']) == {'github': {'type': 'ticket', 'prefix': 'minted'}}

  def test_reference_chain_to_minting_source_pulls_target_without_declaring_it(
    self, tmp_path: Path, ticket_source
  ):
    _write_material(tmp_path, 'brog', '{"token": {"$cred": "github"}}')
    _write_material(tmp_path, 'github', '{"seed": "abc"}')
    _write_sources(tmp_path, {'github': {'type': 'ticket', 'prefix': 'minted'}})

    files, kinds = credentials.build_scoped_store(_store(tmp_path, 'brog', 'github'), {'brog'})

    assert set(files) == {'creds/brog.cred', 'creds/github.cred', 'creds.json'}
    assert kinds == frozenset({'brog'})
    assert json.loads(files['creds.json']) == {'github': {'type': 'ticket', 'prefix': 'minted'}}

  def test_reference_preserving_material_rejects_instance_target(
    self, tmp_path: Path, ticket_source
  ):
    _write_material(tmp_path, 'brog', '{"token": {"$cred": "github+reviewer"}}')
    _write_material(tmp_path, 'github+reviewer', '{"seed": "abc"}')
    _write_sources(tmp_path, {'github+reviewer': {'type': 'ticket', 'prefix': 'minted'}})

    with pytest.raises(ValueError, match='must be spelled at kind level'):
      credentials.build_scoped_store(_store(tmp_path, 'brog', 'github'), {'brog'})

  def test_scoped_view_is_lazy_bounded_and_keeps_selection(self, tmp_path: Path):
    path = _write_material(tmp_path, 'github+reviewer', 'token')
    _write_material(tmp_path, 'openai', 'key')
    source = _store(tmp_path, 'github', 'openai', selection={'github': 'reviewer'})

    view = credentials.scoped_view_store(source, {'github'})
    path.write_text('changed')

    assert view.get('github') == 'changed'
    assert view.try_get('openai') is None
    assert view.known_names() == frozenset({'github', 'openai'})


class TestInstallHooks:
  def _hook_registry(self) -> dict[str, credentials.CredentialKind]:
    return {
      'github': credentials.CredentialKind(
        'github',
        'GitHub credentials',
        install={
          'files': {'token': {'secret': 'github'}},
          'env': {'TOKEN_FILE': {'path': 'token'}},
          'commands': {'gh': {'env': {'GH_TOKEN': {'secret': 'github'}}}},
        },
      )
    }

  def test_applies_only_explicit_kinds_through_the_passed_store(self, tmp_path: Path, monkeypatch):
    store_dir = tmp_path / 'store'
    _write_material(store_dir, 'github+reviewer', 'selected')
    registry = self._hook_registry()
    store = credentials.Store(registry, store_dir, {'github': 'reviewer'})
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'gh').write_text('')
    (binary / 'gh').chmod(0o700)

    exported = credentials.install_hooks(
      registry,
      {'github'},
      store,
      tmp_path / 'environment',
      {'PATH': str(binary)},
    )

    assert (tmp_path / 'environment/token').read_text() == 'selected'
    assert exported['TOKEN_FILE'] == str(tmp_path / 'environment/token')
    wrapper = (tmp_path / 'environment/bin/gh').read_text()
    assert 'credentials get github' in wrapper

  def test_listed_kind_missing_from_store_fails(self, tmp_path: Path):
    registry = self._hook_registry()
    store = credentials.Store(registry, tmp_path / 'store', {})

    with pytest.raises(credentials.SecretNotFound):
      credentials.install_hooks(registry, {'github'}, store, tmp_path / 'environment', {})

  def test_kind_without_hook_is_validated_then_noop(self, tmp_path: Path):
    registry = _registry('openai')
    _write_material(tmp_path / 'store', 'openai', 'key')
    store = credentials.Store(registry, tmp_path / 'store', {})

    assert (
      credentials.install_hooks(registry, {'openai'}, store, tmp_path / 'environment', {}) == {}
    )

  def test_hook_contention_fails(self, tmp_path: Path):
    registry = {
      name: credentials.CredentialKind(
        name, f'{name} credentials', install={'env': {'TOKEN': name}}
      )
      for name in ('one', 'two')
    }
    _write_material(tmp_path / 'store', 'one', '1')
    _write_material(tmp_path / 'store', 'two', '2')
    store = credentials.Store(registry, tmp_path / 'store', {})

    with pytest.raises(ValueError, match='both declare variable TOKEN'):
      credentials.install_hooks(registry, {'one', 'two'}, store, tmp_path / 'environment', {})


class TestDefaultStore:
  def _ambient_store(self, tmp_path: Path, monkeypatch) -> Path:
    store = tmp_path / 'store'
    monkeypatch.delenv('BRO_STORE', raising=False)
    monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
    monkeypatch.setattr(credentials, '_default_store', None)
    monkeypatch.setattr(credentials, 'default_registry', lambda: _registry('openai'))
    monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', str(tmp_path / 'bro.json'))
    return store

  def test_ambient_command_store_merges_the_layers_it_reads(self, tmp_path: Path, monkeypatch):
    store = self._ambient_store(tmp_path, monkeypatch)
    _write_material(store, 'openai+default', 'default')
    _write_material(store, 'openai+benchmark', 'benchmark')
    Path(host_config.HOST_CONFIG_FILE).write_text(
      json.dumps(
        {
          'defaults': {'creds': ['openai+default']},
          'user': {'tools': {'bro.local.benchmark-job': {'creds': ['openai+benchmark']}}},
        }
      )
    )
    monkeypatch.setattr(credentials, 'canonical_cli_name', lambda: 'bro.local.benchmark-job')

    assert credentials.get('openai') == 'benchmark'

  def test_ambient_library_read_uses_defaults_without_a_tool(self, tmp_path: Path, monkeypatch):
    store = self._ambient_store(tmp_path, monkeypatch)
    _write_material(store, 'openai+default', 'default')
    Path(host_config.HOST_CONFIG_FILE).write_text(
      json.dumps({'defaults': {'creds': ['openai+default']}})
    )
    monkeypatch.setattr(credentials, 'canonical_cli_name', lambda: None)

    assert credentials.get('openai') == 'default'

  def test_parser_does_not_read_a_malformed_host_config(self, tmp_path: Path, monkeypatch):
    self._ambient_store(tmp_path, monkeypatch)
    Path(host_config.HOST_CONFIG_FILE).write_text('{')

    assert Parser().parse(['rewind']) == {}

  def test_explicit_bro_store_never_reads_the_host_config(self, tmp_path: Path, monkeypatch):
    store = self._ambient_store(tmp_path, monkeypatch)
    _write_material(store, 'openai', 'directed')
    Path(host_config.HOST_CONFIG_FILE).write_text('{')
    monkeypatch.setenv('BRO_STORE', str(store))
    Parser().parse(['rewind'])

    assert credentials.get('openai') == 'directed'

  def test_unknown_configured_kinds_are_ignored_by_this_installation(
    self, tmp_path: Path, monkeypatch
  ):
    store = self._ambient_store(tmp_path, monkeypatch)
    _write_material(store, 'openai+selected', 'selected')
    Path(host_config.HOST_CONFIG_FILE).write_text(
      json.dumps({'defaults': {'creds': ['consumer_only+host', 'openai+selected']}})
    )
    monkeypatch.setattr(credentials, 'canonical_cli_name', lambda: None)

    assert credentials.get('openai') == 'selected'

  def test_bro_store_is_exclusive(self, tmp_path: Path, monkeypatch):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    _write_material(first, 'openai', 'first')
    _write_material(second, 'openai', 'second')
    monkeypatch.setattr(credentials, 'STORE_DIR', str(first))
    monkeypatch.setattr(credentials, '_default_store', None)
    monkeypatch.setattr(credentials, 'default_registry', lambda: _registry('openai'))

    assert credentials.get('openai') == 'first'

  @pytest.mark.parametrize('filename', ['registry.json', 'credentials.json'])
  def test_retired_file_fails_loudly(self, tmp_path: Path, monkeypatch, filename: str):
    (tmp_path / filename).write_text('{}')
    monkeypatch.setattr(credentials, 'STORE_DIR', str(tmp_path))
    monkeypatch.setattr(credentials, '_default_store', None)

    with pytest.raises(ValueError, match='retired credential file'):
      credentials.default_store()


class TestCli:
  def test_list_prints_registry_descriptions(self, tmp_path: Path, monkeypatch, capsys):
    store = _store(tmp_path, 'github', 'openai')
    monkeypatch.setattr(credentials, 'default_store', lambda: store)

    assert credentials.main(['credentials', 'list']) is None
    assert capsys.readouterr().out.splitlines() == [
      'github: github credential',
      'openai: openai credential',
    ]

  def test_instance_list_enumerates_without_resolving(self, tmp_path: Path, monkeypatch, capsys):
    _write_material(tmp_path, 'github+reviewer', 'token')
    store = _store(tmp_path, 'github')
    monkeypatch.setattr(credentials, 'default_store', lambda: store)

    assert credentials.main(['credentials', 'list', '--instance']) is None
    assert capsys.readouterr().out == 'github+reviewer\n'

  def test_get_instance_reads_exact_storage_name(self, tmp_path: Path, monkeypatch, capsys):
    _write_material(tmp_path, 'github', 'bare')
    _write_material(tmp_path, 'github+reviewer', 'selected')
    store = _store(tmp_path, 'github', selection={'github': 'reviewer'})
    monkeypatch.setattr(credentials, 'default_store', lambda: store)

    assert credentials.main(['credentials', 'get', '--instance', 'github']) is None
    assert capsys.readouterr().out == 'bare\n'
