import importlib.metadata
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

from bro.base import credentials, template


@pytest.fixture
def configs_dir(monkeypatch, tmp_path: Path) -> Path:
  """point the resolver's explicit config root at a tmp dir, isolate the `~/.bro`
  fallback to an empty tmp dir (so the host's real store can't leak in), and reset
  the singleton."""
  configs = tmp_path / 'configs'
  bro = tmp_path / 'bro'
  configs.mkdir()
  bro.mkdir()
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(configs))
  monkeypatch.setattr(credentials, 'BRO_DIR', str(bro))
  monkeypatch.setattr(credentials, '_default_store', None)
  return configs


@pytest.fixture
def bro_dir(configs_dir: Path) -> Path:
  """the `~/.bro` fallback search root set up alongside `configs_dir`."""
  return configs_dir.parent / 'bro'


TEST_SECRET = 'test_secret'
_TEST_SECRET_ENTRY = {
  'sources': [{'file': 'test_secret.json'}],
  'install': 'export TEST_SECRET="$(credentials get \'{{insert #name}}\')"',
}


@pytest.fixture(autouse=True)
def register_test_secret(monkeypatch):
  contributed_registry_data = credentials._contributed_registry_data

  def with_test_secret() -> dict[str, dict]:
    return {**contributed_registry_data(), TEST_SECRET: _TEST_SECRET_ENTRY}

  monkeypatch.setattr(credentials, '_contributed_registry_data', with_test_secret)


def _write(dir: Path, name: str, payload) -> None:
  (dir / name).write_text(payload if isinstance(payload, str) else json.dumps(payload))


class _TicketSource(credentials.MintingSource):
  """concrete minting source for the tests: values `<prefix>_1`, `<prefix>_2`, ...
  (prefix from the config) with a fixed lifetime per instance."""

  TYPE = 'ticket'

  def __init__(self, file: str, *, expires_in: timedelta = timedelta(hours=1)):
    super().__init__(file)
    self.expires_in = expires_in
    self.mints = 0

  def mint(self, config: dict) -> credentials.Minted:
    self.mints += 1
    return credentials.Minted(
      f'{config["prefix"]}_{self.mints}', datetime.now(UTC) + self.expires_in
    )


_REGISTRY_ENTRY = {'sources': [{'file': 'external.json'}]}


def _entry_point(name: str, value: str, group: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, group)


class TestExtensionEntryPoints:
  def test_credential_source_is_discovered_by_type(self, monkeypatch):
    entry_point = _entry_point(
      'ticket', 'bro.base.credentials_test:_TicketSource', credentials._CREDENTIAL_SOURCE_GROUP
    )
    monkeypatch.setattr(
      credentials,
      '_entry_points',
      lambda group: (entry_point,) if group == credentials._CREDENTIAL_SOURCE_GROUP else (),
    )
    source = credentials._source_from_dict({'type': 'ticket', 'file': 'ticket.json'})
    assert isinstance(source, _TicketSource)

  def test_absent_credential_source_has_a_clear_error(self, monkeypatch):
    monkeypatch.setattr(credentials, '_entry_points', lambda group: ())
    with pytest.raises(ValueError, match="unknown credential source type 'ticket'; known"):
      credentials._source_from_dict({'type': 'ticket', 'file': 'ticket.json'})

  def test_registry_entry_is_discovered(self, monkeypatch):
    entry_point = _entry_point(
      'external',
      'bro.base.credentials_test:_REGISTRY_ENTRY',
      credentials._CREDENTIAL_REGISTRY_GROUP,
    )
    monkeypatch.setattr(
      credentials,
      '_entry_points',
      lambda group: (entry_point,) if group == credentials._CREDENTIAL_REGISTRY_GROUP else (),
    )
    registry = credentials.default_registry()
    source = registry['external'].sources[0]
    assert isinstance(source, credentials.LocalSource)
    assert source.file == 'external.json'

  def test_entry_points_use_the_expected_groups(self, monkeypatch):
    calls = []

    def entry_points(**kwargs):
      calls.append(kwargs)
      return ()

    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    credentials._entry_points(credentials._CREDENTIAL_SOURCE_GROUP)
    credentials._entry_points(credentials._CREDENTIAL_REGISTRY_GROUP)
    assert calls == [
      {'group': 'bro.credential_sources'},
      {'group': 'bro.credentials'},
    ]


class TestLocalSource:
  def test_fetch_reads_from_configs_dir(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "t"}'

  def test_fetch_falls_back_to_bro_dir(self, configs_dir: Path, bro_dir: Path):
    _write(bro_dir, 'notion.json', {'token': 'b'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "b"}'

  def test_configs_dir_wins_over_bro_dir(self, configs_dir: Path, bro_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 'c'})
    _write(bro_dir, 'notion.json', {'token': 'b'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "c"}'

  def test_unset_configs_dir_searches_only_bro_dir(self, monkeypatch, bro_dir: Path):
    monkeypatch.setattr(credentials, 'CONFIGS_DIR', None)
    _write(bro_dir, 'notion.json', {'token': 'b'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "b"}'

  def test_fetch_returns_none_when_absent(self, configs_dir: Path):
    assert credentials.LocalSource('missing.json').fetch() is None


class _ParameterNotFound(Exception):
  pass


class TestSSMSource:
  def _client(self, monkeypatch) -> MagicMock:
    client = MagicMock()
    client.exceptions.ParameterNotFound = _ParameterNotFound
    monkeypatch.setattr('boto3.client', MagicMock(return_value=client))
    return client

  def test_fetch_returns_decrypted_parameter_value(self, monkeypatch):
    client = self._client(monkeypatch)
    client.get_parameter.return_value = {'Parameter': {'Value': '{"token": "t"}'}}
    source = credentials.SSMSource('/email-pipeline/notion', 'eu-central-1')
    assert source.fetch() == '{"token": "t"}'
    client.get_parameter.assert_called_once_with(Name='/email-pipeline/notion', WithDecryption=True)

  def test_fetch_names_the_source_region(self, monkeypatch):
    boto3_client = MagicMock()
    boto3_client.return_value.get_parameter.return_value = {'Parameter': {'Value': 'v'}}
    monkeypatch.setattr('boto3.client', boto3_client)
    credentials.SSMSource('/email-pipeline/notion', 'eu-central-1').fetch()
    boto3_client.assert_called_once_with('ssm', region_name='eu-central-1')

  def test_fetch_returns_none_when_parameter_missing(self, monkeypatch):
    client = self._client(monkeypatch)
    client.get_parameter.side_effect = _ParameterNotFound()
    assert credentials.SSMSource('/email-pipeline/notion', 'eu-central-1').fetch() is None

  def test_fetch_propagates_other_errors(self, monkeypatch):
    client = self._client(monkeypatch)
    client.get_parameter.side_effect = RuntimeError('access denied')
    with pytest.raises(RuntimeError, match='access denied'):
      credentials.SSMSource('/email-pipeline/notion', 'eu-central-1').fetch()

  def test_from_dict_requires_region(self):
    with pytest.raises(KeyError):
      credentials.SSMSource.from_dict({'parameter': '/email-pipeline/notion'})

  def test_registry_parses_local_then_ssm_sources(self, configs_dir: Path):
    _write(
      configs_dir,
      credentials.REGISTRY_FILE,
      {
        'notion': {
          'sources': [
            {'file': 'notion.json'},
            {'type': 'ssm', 'parameter': '/email-pipeline/notion', 'region': 'eu-central-1'},
          ]
        }
      },
    )
    registry = credentials._load_registry()
    local, ssm = registry['notion'].sources
    assert isinstance(local, credentials.LocalSource)
    assert isinstance(ssm, credentials.SSMSource)
    assert ssm.parameter == '/email-pipeline/notion'
    assert ssm.region == 'eu-central-1'

  def test_local_file_wins_before_ssm(self, configs_dir: Path, monkeypatch):
    _write(configs_dir, 'notion.json', {'token': 'local'})
    monkeypatch.setattr(
      'boto3.client', MagicMock(side_effect=AssertionError('ssm must not be reached'))
    )
    sources = [
      credentials.LocalSource('notion.json'),
      credentials.SSMSource('/email-pipeline/notion', 'eu-central-1'),
    ]
    store = credentials.Store({'notion': credentials.Secret('notion', sources)})
    assert store.get_json('notion') == {'token': 'local'}

  def test_falls_through_to_ssm_when_no_local_file(self, configs_dir: Path, monkeypatch):
    client = self._client(monkeypatch)
    client.get_parameter.return_value = {'Parameter': {'Value': '{"token": "remote"}'}}
    sources = [
      credentials.LocalSource('notion.json'),
      credentials.SSMSource('/email-pipeline/notion', 'eu-central-1'),
    ]
    store = credentials.Store({'notion': credentials.Secret('notion', sources)})
    assert store.get_json('notion') == {'token': 'remote'}


class TestMintingSource:
  def _source(self, bro_dir: Path, config=None, **kwargs) -> _TicketSource:
    _write(bro_dir, 'ticket.json', config if config is not None else {'prefix': 'ticket'})
    return _TicketSource('ticket.json', **kwargs)

  def test_fetch_mints_from_config(self, bro_dir: Path):
    assert self._source(bro_dir).fetch() == 'ticket_1'

  def test_fetch_returns_none_when_config_absent(self, configs_dir: Path):
    assert _TicketSource('missing.json').fetch() is None

  def test_fetch_reuses_value_until_near_expiry(self, bro_dir: Path):
    source = self._source(bro_dir)
    assert source.fetch() == 'ticket_1'
    assert source.fetch() == 'ticket_1'
    assert source.mints == 1

  def test_fetch_remints_within_expiry_margin(self, bro_dir: Path):
    source = self._source(bro_dir, expires_in=credentials.MintingSource.EXPIRY_MARGIN / 2)
    assert source.fetch() == 'ticket_1'
    assert source.fetch() == 'ticket_2'

  def test_config_not_an_object_raises(self, bro_dir: Path):
    source = self._source(bro_dir, config=[1, 2])
    with pytest.raises(ValueError, match='not a json object'):
      source.fetch()

  def test_config_not_json_raises(self, bro_dir: Path):
    source = self._source(bro_dir, config='not json')
    with pytest.raises(ValueError, match='not valid json'):
      source.fetch()

  def test_materialize_scoped_ships_the_config(self, bro_dir: Path):
    source = self._source(bro_dir)
    entry, content = source.materialize_scoped('github.cred', 'ticket_1')
    assert entry == {'type': 'ticket', 'file': 'github.cred'}
    assert json.loads(content) == {'prefix': 'ticket'}


class TestRegistryOverride:
  def test_environment_override_wins_over_search_dirs(self, configs_dir: Path, monkeypatch):
    _write(configs_dir, 'shadowed.json', {'token': 'shadowed'})
    _write(
      configs_dir,
      credentials.REGISTRY_FILE,
      {'notion': {'sources': [{'file': 'shadowed.json'}]}},
    )
    _write(configs_dir, 'override.json', {'token': 'override'})
    registry_path = configs_dir / 'explicit_registry.json'
    registry_path.write_text(json.dumps({'notion': {'sources': [{'file': 'override.json'}]}}))
    monkeypatch.setenv('CREDENTIALS_REGISTRY', str(registry_path))
    assert credentials.default_store().get_json('notion') == {'token': 'override'}

  def test_environment_override_bounds_the_registry(self, configs_dir: Path, monkeypatch):
    _write(configs_dir, 'notion.json', {'token': 't'})
    registry_path = configs_dir / 'explicit_registry.json'
    registry_path.write_text(json.dumps({'openai': {'sources': [{'file': 'openai.json'}]}}))
    monkeypatch.setenv('CREDENTIALS_REGISTRY', str(registry_path))
    with pytest.raises(credentials.SecretNotFound):
      credentials.default_store().get('notion')

  def test_override_directory_joins_the_search_path(self, configs_dir: Path, tmp_path, monkeypatch):
    # a materialized scoped store is a registry plus its sibling `.cred` files in
    # one dir (outside the standard search dirs on host) — the override's own
    # directory must be searched for the store to resolve
    store_dir = tmp_path / 'scoped-store'
    store_dir.mkdir()
    (store_dir / 'notion.cred').write_text('scoped-value')
    registry_path = store_dir / credentials.REGISTRY_FILE
    registry_path.write_text(json.dumps({'notion': {'sources': [{'file': 'notion.cred'}]}}))
    monkeypatch.setenv('CREDENTIALS_REGISTRY', str(registry_path))
    assert credentials.default_store().get('notion') == 'scoped-value'

  def test_bad_override_path_raises(self, configs_dir: Path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_REGISTRY', str(configs_dir / 'absent_registry.json'))
    with pytest.raises(FileNotFoundError):
      credentials._load_registry()

  def test_empty_override_is_ignored(self, configs_dir: Path, monkeypatch):
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    monkeypatch.setenv('CREDENTIALS_REGISTRY', '')
    assert credentials.default_store().get_json(TEST_SECRET) == {'token': 't'}


class TestStore:
  def _store(self, *secrets: credentials.Secret) -> credentials.Store:
    return credentials.Store({s.name: s for s in secrets})

  def test_get_returns_raw_text(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't', 'tasks_db_id': 'd'})
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.get('notion') == '{"token": "t", "tasks_db_id": "d"}'

  def test_get_strips_trailing_whitespace(self, configs_dir: Path):
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc\n')
    store = self._store(
      credentials.Secret('github', [credentials.LocalSource('cw_github_token_bro')])
    )
    assert store.get('github') == 'ghp_abc'

  def test_first_source_with_value_wins(self, configs_dir: Path):
    _write(configs_dir, 'second.json', {'who': 'second'})
    sources = [credentials.LocalSource('first.json'), credentials.LocalSource('second.json')]
    store = self._store(credentials.Secret('s', sources))
    assert store.get('s') == '{"who": "second"}'

  def test_value_is_cached(self, configs_dir: Path):
    path = configs_dir / 'notion.json'
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.get('notion') == '{"token": "t"}'
    path.unlink()
    assert store.get('notion') == '{"token": "t"}'

  def test_uncacheable_source_refetches_every_get(self, configs_dir: Path):
    # a source that mints short-lived values owns its own refresh; the store
    # must consult it on every read instead of caching the first mint
    values = iter(['first', 'second'])

    class _MintingSource:
      CACHEABLE: ClassVar[bool] = False

      def fetch(self) -> Optional[str]:
        return next(values)

      def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
        return {'file': file}, value.encode()

    store = self._store(credentials.Secret('s', [_MintingSource()]))
    assert store.get('s') == 'first'
    assert store.get('s') == 'second'

  def test_unknown_name_raises(self):
    with pytest.raises(credentials.SecretNotFound) as exception:
      credentials.Store({}).get('nope')
    assert exception.value.name == 'nope'

  def test_resolve_flags_stored_value_cacheable(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.resolve('notion') == ('{"token": "t"}', True)

  def test_resolve_flags_minted_value_uncacheable(self, bro_dir: Path):
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    store = self._store(credentials.Secret('sekret', [_TicketSource('ticket.json')]))
    assert store.resolve('sekret') == ('ticket_1', False)

  def test_resolve_returns_none_when_unresolvable(self, configs_dir: Path):
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.resolve('notion') is None

  def test_try_get_returns_value_when_resolvable(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.try_get('notion') == '{"token": "t"}'

  def test_try_get_returns_none_when_unresolvable(self, configs_dir: Path):
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.try_get('notion') is None

  def test_try_get_returns_none_for_unknown_name(self):
    assert credentials.Store({}).try_get('nope') is None

  def test_available_true_when_resolvable(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.available('notion') is True

  def test_available_false_when_unresolvable(self, configs_dir: Path):
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.available('notion') is False

  def test_available_false_for_unknown_name(self):
    assert credentials.Store({}).available('nope') is False

  def test_no_source_has_value_raises(self, configs_dir: Path):
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    with pytest.raises(credentials.SecretNotFound) as exception:
      store.get('notion')
    assert exception.value.name == 'notion'

  def test_get_json_parses_to_dict(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    assert store.get_json('notion') == {'token': 't'}

  def test_get_json_rejects_non_json(self, configs_dir: Path):
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc')
    store = self._store(
      credentials.Secret('github', [credentials.LocalSource('cw_github_token_bro')])
    )
    with pytest.raises(ValueError, match='not valid json'):
      store.get_json('github')

  def test_get_json_rejects_non_object(self, configs_dir: Path):
    _write(configs_dir, 'arr.json', [1, 2, 3])
    store = self._store(credentials.Secret('arr', [credentials.LocalSource('arr.json')]))
    with pytest.raises(ValueError, match='not a json object'):
      store.get_json('arr')

  def test_concurrent_get_fetches_once(self):
    # 8 threads resolve the same uncached secret at once. the lock makes
    # resolution single-flight, so the source is fetched exactly once; the
    # sleep widens the cache-miss window so a missing lock would duplicate it.
    calls: list[int] = []

    class _CountingSource:
      CACHEABLE: ClassVar[bool] = True

      def fetch(self) -> Optional[str]:
        calls.append(1)  # list.append is atomic under the GIL
        time.sleep(0.02)
        return 'v'

      def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
        return {'file': file}, value.encode()

    store = self._store(credentials.Secret('s', [_CountingSource()]))
    results: list[str] = []

    def worker() -> None:
      results.append(store.get('s'))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert results == ['v'] * 8
    assert len(calls) == 1


class TestReferences:
  def _store(self, configs_dir: Path, secrets: dict[str, object]) -> credentials.Store:
    """a store over local secrets, each payload written to its own file."""
    registry = {}
    for name, payload in secrets.items():
      file = f'{name}.secret'
      _write(configs_dir, file, payload)
      registry[name] = credentials.Secret(name, [credentials.LocalSource(file)])
    return credentials.Store(registry)

  def test_object_reference_embeds_the_parsed_value(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {
        'notion': {'token': 't', 'tasks_db_id': 'd'},
        'brog': {'backend': 'flow', 'transport': 'local', 'notion': {'$cred': 'notion'}},
      },
    )
    assert store.get_json('brog') == {
      'backend': 'flow',
      'transport': 'local',
      'notion': {'token': 't', 'tasks_db_id': 'd'},
    }

  def test_field_reference_picks_one_field(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {
        'flow_mcp': {'url': 'https://flow.example', 'token': 'tok'},
        'brog': {
          'backend': 'flow',
          'transport': 'http',
          'url': {'$cred': 'flow_mcp', 'field': 'url'},
          'token': {'$cred': 'flow_mcp', 'field': 'token'},
        },
      },
    )
    assert store.get_json('brog') == {
      'backend': 'flow',
      'transport': 'http',
      'url': 'https://flow.example',
      'token': 'tok',
    }

  def test_scalar_secret_substitutes_as_string(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {
        'github': 'ghp_abc\n',
        'brog': {'backend': 'github', 'token': {'$cred': 'github'}},
      },
    )
    assert store.get_json('brog') == {'backend': 'github', 'token': 'ghp_abc'}

  def test_numeric_scalar_substitutes_as_string(self, configs_dir: Path):
    # only a json *object* substitutes parsed; a token that happens to parse as a
    # json scalar ('12345') must stay a string, not become a number
    store = self._store(configs_dir, {'pin': '12345', 'config': {'value': {'$cred': 'pin'}}})
    assert store.get_json('config') == {'value': '12345'}

  def test_variant_target_resolves(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {
        'github+alice': 'ghp_alice',
        'brog': {'backend': 'github', 'token': {'$cred': 'github+alice'}},
      },
    )
    assert store.get_json('brog') == {'backend': 'github', 'token': 'ghp_alice'}

  def test_references_nest_in_arrays_and_objects(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {
        'a': 'v',
        'config': {'outer': {'inner': [{'$cred': 'a'}, 'plain']}},
      },
    )
    assert store.get_json('config') == {'outer': {'inner': ['v', 'plain']}}

  def test_transitive_references_expand(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {
        'c': 'v',
        'b': {'c': {'$cred': 'c'}},
        'a': {'b': {'$cred': 'b'}},
      },
    )
    assert store.get_json('a') == {'b': {'c': 'v'}}

  def test_no_references_passes_through_byte_identical(self, configs_dir: Path):
    store = self._store(configs_dir, {'x': '{"a":   1}'})
    assert store.get('x') == '{"a":   1}'

  def test_non_json_text_is_untouched(self, configs_dir: Path):
    store = self._store(configs_dir, {'x': 'not json, even with {"$cred": "y"} inside'})
    assert store.get('x') == 'not json, even with {"$cred": "y"} inside'

  def test_cycle_raises(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {'a': {'x': {'$cred': 'b'}}, 'b': {'y': {'$cred': 'a'}}},
    )
    with pytest.raises(ValueError, match=r'cycle: a -> b -> a'):
      store.get('a')

  def test_self_reference_raises(self, configs_dir: Path):
    store = self._store(configs_dir, {'a': {'x': {'$cred': 'a'}}})
    with pytest.raises(ValueError, match=r'cycle: a -> a'):
      store.get('a')

  def test_unresolvable_reference_raises(self, configs_dir: Path):
    store = self._store(configs_dir, {'config': {'token': {'$cred': 'nope'}}})
    with pytest.raises(ValueError, match="'config' references 'nope', which does not resolve"):
      store.get('config')

  def test_broken_reference_raises_even_via_try_get(self, configs_dir: Path):
    # try_get is non-raising for *absent* secrets only; a present secret with a
    # broken reference is corruption, not absence
    store = self._store(configs_dir, {'config': {'token': {'$cred': 'nope'}}})
    with pytest.raises(ValueError, match='does not resolve'):
      store.try_get('config')

  def test_field_of_scalar_secret_raises(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {'github': 'ghp_abc', 'config': {'token': {'$cred': 'github', 'field': 'token'}}},
    )
    with pytest.raises(ValueError, match='not a json object'):
      store.get('config')

  def test_missing_field_raises(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {'flow_mcp': {'url': 'u'}, 'config': {'token': {'$cred': 'flow_mcp', 'field': 'token'}}},
    )
    with pytest.raises(ValueError, match="has no field 'token'"):
      store.get('config')

  def test_unknown_reference_keys_raise(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {'a': 'v', 'config': {'token': {'$cred': 'a', 'transform': 'upper'}}},
    )
    with pytest.raises(ValueError, match="unknown keys: 'transform'"):
      store.get('config')

  def test_non_string_reference_name_raises(self, configs_dir: Path):
    store = self._store(configs_dir, {'config': {'token': {'$cred': 7}}})
    with pytest.raises(ValueError, match='reference name must be a string'):
      store.get('config')

  def test_non_string_field_raises(self, configs_dir: Path):
    store = self._store(
      configs_dir,
      {'a': {'k': 'v'}, 'config': {'token': {'$cred': 'a', 'field': 1}}},
    )
    with pytest.raises(ValueError, match='reference field must be a string'):
      store.get('config')

  def test_scoped_store_materializes_expanded_text(self, configs_dir: Path, monkeypatch):
    # hydration resolves through the store, so the materialized `.cred` is the
    # expanded, self-contained value — the container never sees the references
    # and needs no grant of the referenced secrets
    registry = {}
    for name, payload in {
      'notion': {'token': 't'},
      'brog': {'backend': 'flow', 'transport': 'local', 'notion': {'$cred': 'notion'}},
    }.items():
      file = f'{name}.secret'
      _write(configs_dir, file, payload)
      registry[name] = credentials.Secret(name, [credentials.LocalSource(file)])
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['brog'])
    assert set(store) == {'brog.cred', credentials.REGISTRY_FILE}
    assert json.loads(store['brog.cred']) == {
      'backend': 'flow',
      'transport': 'local',
      'notion': {'token': 't'},
    }
    assert b'$cred' not in store['brog.cred']

  def test_reference_to_uncacheable_secret_reexpands_every_get(self, configs_dir: Path):
    # a referrer that embedded a minted value must not cache it past the mint's
    # own refresh — non-cacheability propagates through the expansion
    tokens = iter(['t1', 't2'])

    class _MintingSource:
      CACHEABLE: ClassVar[bool] = False

      def fetch(self) -> Optional[str]:
        return next(tokens)

      def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
        return {'file': file}, value.encode()

    _write(configs_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    store = credentials.Store(
      {
        'github': credentials.Secret('github', [_MintingSource()]),
        'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
      }
    )
    assert store.get_json('brog')['token'] == 't1'
    assert store.get_json('brog')['token'] == 't2'


class TestDefaultRegistry:
  def test_inventory_covers_known_secrets(self):
    registry = credentials.default_registry()
    names = (
      'anthropic',
      'brave',
      'brog',
      'claude_code',
      'github',
      'openai',
      TEST_SECRET,
      'trails',
    )
    for name in names:
      assert name in registry

  def test_install_file_reference_inlines_the_hook_file(self):
    # the github entry declares its hook as {"file": ...}; the load inlines the
    # shell file's text, so the rendered hook carries its content
    install = credentials.default_registry()['github'].install
    assert install is not None
    assert '.local/bin/gh' in install

  def test_install_file_reference_rejected_outside_the_builtin_registry(self):
    with pytest.raises(ValueError, match='install must be a string'):
      credentials.Secret.from_dict('x', {'sources': [{'file': 'f'}], 'install': {'file': 'h.sh'}})

  def test_github_declares_no_builtin_source(self):
    # the github kind's sources are host-local (the app minting config in the
    # host registry file); the checked-in entry carries only the install hook
    registry = credentials.default_registry()
    assert registry['github'].sources == []
    assert registry['github'].install is not None

  def test_builtin_hooks_render_the_name_template(self):
    # the checked-in hooks are `{{insert #name}}` templates; a plain kind entry
    # renders with its own name in the single-quoted insert slot
    registry = credentials.default_registry()
    assert registry['github'].install is not None
    assert 'credentials get github' in registry['github'].install
    assert '{{' not in registry['github'].install
    # non-directive braces are literal text to the engine — the shell function
    # body in the credential-helper line survives rendering
    assert '{ echo username=x-access-token' in registry['github'].install

  def test_hook_with_unknown_template_variable_raises(self):
    with pytest.raises(template.TemplateError, match='unknown variable'):
      credentials.Secret.from_dict(
        'x', {'sources': [{'file': 'f'}], 'install': 'echo {{insert #nope}}'}
      )


class TestNameGrammar:
  def test_plain_name_is_its_own_kind(self):
    assert credentials.parse_name('github') == ('github', None)

  def test_variant_name_splits_kind_and_instance(self):
    assert credentials.parse_name('github+alice') == ('github', 'alice')

  def test_instance_allows_dashes(self):
    assert credentials.parse_name('github+read-only') == ('github', 'read-only')

  @pytest.mark.parametrize(
    'name',
    ['github+', '+alice', 'github+a+b', 'GitHub+a', 'github+Alice', 'github[alice]', 'git hub', ''],
  )
  def test_malformed_name_raises(self, name: str):
    with pytest.raises(ValueError, match='malformed secret name'):
      credentials.parse_name(name)


class TestHostRegistry:
  def test_absent_additions_file_yields_builtin(self, bro_dir: Path):
    assert set(credentials.host_registry()) == set(credentials.default_registry())

  def test_additions_merge_per_name_over_builtin(self, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    registry = credentials.host_registry()
    assert 'github+alice' in registry
    # the built-in entries survive the merge untouched
    assert 'github' in registry
    assert TEST_SECRET in registry

  def test_additions_file_follows_the_search_path_priority(self, configs_dir: Path, bro_dir: Path):
    # like any secret file: the first search dir that has it wins — the file in
    # the explicit config dir shadows the one in `~/.bro`, not merged with it
    _write(
      configs_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+work': {'sources': [{'file': 'a'}]}},
    )
    _write(bro_dir, credentials.HOST_REGISTRY_FILE, {'github+home': {'sources': [{'file': 'b'}]}})
    registry = credentials.host_registry()
    assert 'github+work' in registry
    assert 'github+home' not in registry

  def test_addition_replaces_a_registry_entry_wholesale(self, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {TEST_SECRET: {'sources': [{'file': 'replacement.json'}]}},
    )
    source = credentials.host_registry()[TEST_SECRET].sources[0]
    assert isinstance(source, credentials.LocalSource)
    assert source.file == 'replacement.json'

  def test_kind_override_without_install_inherits_the_builtin_hook(self, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github': {'sources': [{'type': 'github_app', 'file': 'github_app_x.json'}]}},
    )
    registry = credentials.host_registry()
    source = registry['github'].sources[0]
    assert isinstance(source, credentials.MintingSource)
    assert source.file == 'github_app_x.json'
    assert registry['github'].install == credentials.default_registry()['github'].install

  def test_variant_inherits_the_kind_hook_instantiated_with_its_name(self, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    registry = credentials.host_registry()
    variant = registry['github+alice'].install
    assert variant is not None
    assert 'credentials get github+alice' in variant
    # the kind's own hook still names the kind
    kind = registry['github'].install
    assert kind is not None
    assert 'credentials get github' in kind

  def test_variant_of_hookless_kind_has_no_hook(self, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'openai+work': {'sources': [{'file': 'openai_work.json'}]}},
    )
    assert credentials.host_registry()['openai+work'].install is None

  def test_variant_declaring_its_own_install_raises(self, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'f'}], 'install': 'export X=1'}},
    )
    with pytest.raises(ValueError, match='the kind entry owns it'):
      credentials.host_registry()

  def test_variant_of_unknown_kind_raises(self, bro_dir: Path):
    _write(bro_dir, credentials.HOST_REGISTRY_FILE, {'nope+x': {'sources': [{'file': 'f'}]}})
    with pytest.raises(ValueError, match="no kind entry 'nope'"):
      credentials.host_registry()

  def test_malformed_addition_name_raises(self, bro_dir: Path):
    _write(bro_dir, credentials.HOST_REGISTRY_FILE, {'GitHub+alice': {'sources': [{'file': 'f'}]}})
    with pytest.raises(ValueError, match='malformed secret name'):
      credentials.host_registry()

  def test_default_store_resolves_a_host_local_variant(self, configs_dir: Path, bro_dir: Path):
    # end-to-end through _load_registry: a host-local variant resolves like any
    # other secret
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    _write(bro_dir, 'github_token_alice', 'ghp_alice\n')
    assert credentials.default_store().get('github+alice') == 'ghp_alice'

  def test_generated_registry_still_replaces_wholesale(self, configs_dir: Path, bro_dir: Path):
    # a generated credentials.json bounds the registry to exactly its own set —
    # host-local additions must not leak through it (the scoped-store bounding
    # invariant)
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    _write(bro_dir, credentials.REGISTRY_FILE, {'notion': {'sources': [{'file': 'notion.json'}]}})
    assert set(credentials._load_registry()) == {'notion'}


class TestDefaultStore:
  def test_falls_back_to_builtin_registry(self, configs_dir: Path):
    _write(configs_dir, 'openai.json', {'token': 't'})
    assert credentials.default_store().get_json('openai') == {'token': 't'}

  def test_credentials_json_overrides_builtin(self, configs_dir: Path):
    _write(configs_dir, 'custom.json', {'token': 'x'})
    _write(
      configs_dir,
      credentials.REGISTRY_FILE,
      {'notion': {'sources': [{'type': 'local', 'file': 'custom.json'}]}},
    )
    assert credentials.default_store().get_json('notion') == {'token': 'x'}

  def test_source_type_defaults_to_local(self, configs_dir: Path):
    # a source dict may omit `type`; it defaults to `local`
    _write(configs_dir, 'custom.json', {'token': 'x'})
    _write(
      configs_dir,
      credentials.REGISTRY_FILE,
      {'notion': {'sources': [{'file': 'custom.json'}]}},
    )
    assert credentials.default_store().get_json('notion') == {'token': 'x'}

  def test_credentials_json_in_bro_dir_overrides_builtin(self, configs_dir: Path, bro_dir: Path):
    # a scoped credentials.json mounted at the container's ~/.bro takes effect:
    # the registry load searches that dir too (no file in the explicit config dir there).
    _write(bro_dir, 'custom.json', {'token': 'p'})
    _write(
      bro_dir,
      credentials.REGISTRY_FILE,
      {'notion': {'sources': [{'type': 'local', 'file': 'custom.json'}]}},
    )
    registry = credentials._load_registry()
    assert set(registry) == {'notion'}
    assert credentials.default_store().get_json('notion') == {'token': 'p'}


class TestModuleAliases:
  def test_get_json_aliases_default_store(self, configs_dir: Path):
    _write(configs_dir, 'openai.json', {'token': 't'})
    assert credentials.get_json('openai') == {'token': 't'}

  def test_get_aliases_default_store_raw_text(self, configs_dir: Path):
    # `claude_code` maps to a raw-text file; the alias returns it stripped, like the store.
    _write(configs_dir, 'claude_code_oauth_token', 'tok\n')
    assert credentials.get('claude_code') == 'tok'

  def test_get_raises_secret_not_found(self, configs_dir: Path):
    with pytest.raises(credentials.SecretNotFound):
      credentials.get('brave')

  def test_try_get_aliases_default_store(self, configs_dir: Path):
    _write(configs_dir, 'claude_code_oauth_token', 'tok\n')
    assert credentials.try_get('claude_code') == 'tok'
    assert credentials.try_get('brave') is None

  def test_available_aliases_default_store(self, configs_dir: Path):
    _write(configs_dir, 'openai.json', {'token': 't'})
    assert credentials.available('openai') is True
    assert credentials.available('brave') is False


class TestCLI:
  def test_list_prints_sorted_available_names(self, configs_dir: Path, capsys):
    _write(configs_dir, 'openai.json', {'token': 't'})
    _write(configs_dir, 'claude_code_oauth_token', 'tok-abc')

    assert credentials.main(['credentials', 'list']) is None
    assert capsys.readouterr().out == 'claude_code\nopenai\n'

  def test_get_json_prints_json(self, configs_dir: Path, capsys):
    _write(configs_dir, 'openai.json', {'token': 't'})
    assert credentials.main(['credentials', 'get', 'openai']) is None
    assert json.loads(capsys.readouterr().out) == {'token': 't'}

  def test_get_field_prints_value(self, configs_dir: Path, capsys):
    _write(configs_dir, 'anthropic.json', {'api_key': 'sk-xyz'})
    assert credentials.main(['credentials', 'get', 'anthropic', '--field', 'api_key']) is None
    assert capsys.readouterr().out.strip() == 'sk-xyz'

  def test_get_text_prints_string(self, configs_dir: Path, capsys):
    _write(configs_dir, 'claude_code_oauth_token', 'tok-abc\n')
    assert credentials.main(['credentials', 'get', 'claude_code']) is None
    assert capsys.readouterr().out.strip() == 'tok-abc'

  def test_missing_secret_exits_nonzero(self, configs_dir: Path, capsys):
    assert credentials.main(['credentials', 'get', 'brave']) == 1
    assert 'not found' in capsys.readouterr().err

  def test_field_on_non_json_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'claude_code_oauth_token', 'tok-abc')
    assert credentials.main(['credentials', 'get', 'claude_code', '--field', 'api_key']) == 1
    assert 'not valid json' in capsys.readouterr().err

  def test_missing_field_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'anthropic.json', {'other': 'x'})
    assert credentials.main(['credentials', 'get', 'anthropic', '--field', 'api_key']) == 1
    assert 'no field' in capsys.readouterr().err

  def test_json_flag_pretty_prints(self, configs_dir: Path, capsys):
    _write(configs_dir, 'openai.json', {'token': 't', 'db': 'd'})
    assert credentials.main(['credentials', 'get', 'openai', '--json']) is None
    out = capsys.readouterr().out
    assert json.loads(out) == {'token': 't', 'db': 'd'}
    assert '\n  ' in out  # indent=2

  def test_json_flag_on_non_json_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'claude_code_oauth_token', 'tok-abc')
    assert credentials.main(['credentials', 'get', 'claude_code', '--json']) == 1
    assert 'not valid json' in capsys.readouterr().err


class TestBuildScopedStore:
  def test_builds_files_and_scoped_registry(self, configs_dir: Path):
    _write(configs_dir, 'openai.json', {'token': 't'})
    _write(configs_dir, 'claude_code_oauth_token', 'tok-abc\n')
    store = credentials.build_scoped_store(['openai', 'claude_code'])
    # one `{name}.cred` entry per secret, plus the scoped registry
    assert set(store) == {'openai.cred', 'claude_code.cred', credentials.REGISTRY_FILE}
    # each secret's raw text (stripped) as bytes
    assert json.loads(store['openai.cred']) == {'token': 't'}
    assert store['claude_code.cred'] == b'tok-abc'
    registry = json.loads(store[credentials.REGISTRY_FILE])
    assert set(registry) == {'openai', 'claude_code'}
    # the install hook rides along so the container can apply it generically; the
    # source omits `type` (local is the default) and points at the scoped file
    assert registry['claude_code']['sources'] == [{'file': 'claude_code.cred'}]
    assert 'install' in registry['claude_code']
    # a secret with no install hook carries none
    assert 'install' not in registry['openai']

  def test_non_local_source_round_trips_to_scoped_local_file(self, configs_dir: Path, monkeypatch):
    # a pure non-local secret (no LocalSource) hydrates the same as a local one:
    # the value resolves generically via store.get on the host and materializes as
    # a scoped local `{name}.cred`, so the container reads a plain local file with
    # no idea the host source was remote.
    class _StubSource:
      CACHEABLE: ClassVar[bool] = True

      def fetch(self) -> Optional[str]:
        return 'sekret'

      def materialize_scoped(self, file: str, value: str) -> tuple[dict, bytes]:
        return {'file': file}, value.encode()

    registry = {'remote': credentials.Secret('remote', [_StubSource()])}
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['remote'])
    assert set(store) == {'remote.cred', credentials.REGISTRY_FILE}
    assert store['remote.cred'] == b'sekret'
    scoped = json.loads(store[credentials.REGISTRY_FILE])
    # declared as a plain local source pointing at the materialized file...
    assert scoped['remote']['sources'] == [{'file': 'remote.cred'}]
    # ...and that scoped entry rehydrates as a LocalSource (type defaults to local)
    rebuilt = credentials._registry_from_dict(scoped)
    assert isinstance(rebuilt['remote'].sources[0], credentials.LocalSource)

  def test_minting_secret_ships_the_config(self, bro_dir: Path, monkeypatch):
    # the scoped store carries the minting config, not a minted value — the
    # session re-derives on read; the host-side resolve at build time is the
    # launch validation of the config
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    source = _TicketSource('ticket.json')
    registry = {'github+bot': credentials.Secret('github+bot', [source])}
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['github+bot'])
    assert source.mints == 1
    # the variant materializes under its kind, keeping the source's own type
    assert json.loads(store['github.cred']) == {'prefix': 'ticket'}
    scoped = json.loads(store[credentials.REGISTRY_FILE])
    assert scoped['github']['sources'] == [{'type': 'ticket', 'file': 'github.cred'}]

  def test_minting_failure_fails_the_build(self, bro_dir: Path, monkeypatch):
    class _BrokenSource(_TicketSource):
      def mint(self, config: dict) -> credentials.Minted:
        raise ValueError('bad key')

    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    registry = {'sekret': credentials.Secret('sekret', [_BrokenSource('ticket.json')])}
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    with pytest.raises(ValueError, match='bad key'):
      credentials.build_scoped_store(['sekret'])

  def test_fallback_list_materializes_the_winning_source(self, bro_dir: Path, monkeypatch):
    # an ordered [minting, local] list collapses to whichever source resolves:
    # with the minting config absent, the local fallback wins and ships its value
    _write(bro_dir, 'token_file', 'ghp_static')
    sources = [_TicketSource('absent.json'), credentials.LocalSource('token_file')]
    registry = {'github': credentials.Secret('github', sources)}
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['github'])
    assert store['github.cred'] == b'ghp_static'
    scoped = json.loads(store[credentials.REGISTRY_FILE])
    assert scoped['github']['sources'] == [{'file': 'github.cred'}]

  def test_uncacheable_expansion_ships_references_intact(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # a `$cred` chain reaching a minting source must not freeze the minted value
    # into the store: the referrer ships its raw text, references intact, so the
    # session re-expands per read against the scoped registry and mints fresh
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(configs_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    source = _TicketSource('ticket.json')
    registry = {
      'github': credentials.Secret('github', [source]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['brog', 'github'])
    # the host-side resolve still validated the chain — and minted exactly once
    assert source.mints == 1
    assert b'$cred' in store['brog.cred']
    assert json.loads(store['brog.cred']) == {'backend': 'github', 'token': {'$cred': 'github'}}
    assert json.loads(store['github.cred']) == {'prefix': 'ticket'}
    scoped = json.loads(store[credentials.REGISTRY_FILE])
    assert scoped['brog']['sources'] == [{'file': 'brog.cred'}]
    assert scoped['github']['sources'] == [{'type': 'ticket', 'file': 'github.cred'}]

  def test_raw_shipped_secret_mints_fresh_in_session(
    self, configs_dir: Path, bro_dir: Path, monkeypatch, tmp_path: Path
  ):
    # end-to-end: land the scoped store on disk and resolve as the container
    # would — each read re-expands the shipped references and observes a fresh
    # mint from the shipped minting config
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(configs_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github': credentials.Secret('github', [_TicketSource('ticket.json')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    original_load_registry = credentials._load_registry
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    files = credentials.build_scoped_store(['brog', 'github'])
    dest = tmp_path / 'scoped'
    dest.mkdir()
    for filename, data in files.items():
      (dest / filename).write_bytes(data)
    monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path / 'absent'))
    monkeypatch.setattr(credentials, 'BRO_DIR', str(dest))
    monkeypatch.setattr(credentials, '_default_store', None)
    # back to the real loader: the session resolves via the landed credentials.json
    monkeypatch.setattr(credentials, '_load_registry', original_load_registry)
    # give the scoped registry's `ticket` type a dispatch branch; zero lifetime
    # makes every fetch a fresh mint, exposing whether reads re-expand
    original_dispatch = credentials._source_from_dict

    def dispatch(data: dict) -> credentials.Source:
      if data.get('type') == _TicketSource.TYPE:
        return _TicketSource(data['file'], expires_in=timedelta(0))
      return original_dispatch(data)

    monkeypatch.setattr(credentials, '_source_from_dict', dispatch)
    session_store = credentials.default_store()
    assert session_store.get_json('brog')['token'] == 'ticket_1'
    assert session_store.get_json('brog')['token'] == 'ticket_2'

  def test_variant_referrer_ships_raw_under_its_kind(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # the kap topology: the launch selects the `brog+github` variant, whose
    # config references the `github` kind backed by a minting source — the
    # variant's raw text lands under `brog.cred`, references intact
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(bro_dir, 'brog_github.json', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github': credentials.Secret('github', [_TicketSource('ticket.json')]),
      'brog+github': credentials.Secret(
        'brog+github', [credentials.LocalSource('brog_github.json')]
      ),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['brog+github', 'github'])
    assert set(store) == {'brog.cred', 'github.cred', credentials.REGISTRY_FILE}
    assert b'$cred' in store['brog.cred']
    assert set(json.loads(store[credentials.REGISTRY_FILE])) == {'brog', 'github'}

  def test_cacheable_expansion_resolves_through_the_selected_instance(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # a kind-level `$cred` reference in a cacheable chain freezes the instance
    # the scope selected — the same value the session's own `.cred` for that
    # kind carries, and the same answer the uncacheable path's in-session
    # re-expansion would give
    _write(bro_dir, 'github_token_default', 'ghp_default')
    _write(bro_dir, 'github_token_bot', 'ghp_bot')
    _write(bro_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github': credentials.Secret('github', [credentials.LocalSource('github_token_default')]),
      'github+bot': credentials.Secret('github+bot', [credentials.LocalSource('github_token_bot')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['brog', 'github+bot'])
    assert json.loads(store['brog.cred']) == {'backend': 'github', 'token': 'ghp_bot'}
    assert store['github.cred'] == b'ghp_bot'

  def test_cacheable_expansion_outside_the_scope_falls_through_to_the_registry(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # only in-scope kinds are rebound; a reference to a kind outside the scope
    # expands against the registry's own entry and freezes self-contained
    _write(bro_dir, 'github_token_default', 'ghp_default')
    _write(bro_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github': credentials.Secret('github', [credentials.LocalSource('github_token_default')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.build_scoped_store(['brog'])
    assert json.loads(store['brog.cred']) == {'backend': 'github', 'token': 'ghp_default'}
    assert set(store) == {'brog.cred', credentials.REGISTRY_FILE}

  def test_shipped_reference_outside_scope_fails(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # host-side the reference resolves (the host registry has the kind), but the
    # session couldn't re-expand it — the referenced kind must be hydrated too
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(configs_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github': credentials.Secret('github', [_TicketSource('ticket.json')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    with pytest.raises(ValueError, match="'github' is not in the scoped set"):
      credentials.build_scoped_store(['brog'])

  def test_optional_shipped_reference_outside_scope_fails(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # the optional tier forgives absence, not misconfiguration: a resolvable
    # optional secret whose shipped references escape the scope fails the build
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(configs_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github': credentials.Secret('github', [_TicketSource('ticket.json')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    with pytest.raises(ValueError, match="'github' is not in the scoped set"):
      credentials.build_scoped_store([], optional=['brog'])

  def test_shipped_reference_must_be_kind_level(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # an instance-spelled reference can never resolve in the kinds-only scoped
    # namespace, even when the launch selected exactly that instance
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(configs_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github+bot'}})
    registry = {
      'github+bot': credentials.Secret('github+bot', [_TicketSource('ticket.json')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    with pytest.raises(ValueError, match='kind level'):
      credentials.build_scoped_store(['brog', 'github+bot'])

  def test_empty_names_yields_only_registry(self, configs_dir: Path):
    # cw always cps a store in (even a zero-secret session), so the registry
    # file is always present — an empty bounding registry.
    store = credentials.build_scoped_store([])
    assert set(store) == {credentials.REGISTRY_FILE}
    assert json.loads(store[credentials.REGISTRY_FILE]) == {}

  def test_scoped_store_bounds_container_registry(
    self, configs_dir: Path, monkeypatch, tmp_path: Path
  ):
    # materialising the store as a container's ~/.bro bounds it to the built
    # set: a non-declared secret resolves to a clean SecretNotFound.
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    _write(configs_dir, 'brave.json', {'api_key': 'k'})
    dest = tmp_path / 'scoped'
    dest.mkdir()
    for filename, data in credentials.build_scoped_store([TEST_SECRET]).items():
      (dest / filename).write_bytes(data)
    # resolve as the container would: scoped dir is its ~/.bro and the explicit
    # config dir is absent. the scoped credentials.json bounds the registry.
    monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path / 'absent'))
    monkeypatch.setattr(credentials, 'BRO_DIR', str(dest))
    monkeypatch.setattr(credentials, '_default_store', None)
    assert credentials.default_store().get_json(TEST_SECRET) == {'token': 't'}
    with pytest.raises(credentials.SecretNotFound):
      credentials.default_store().get('brave')

  def test_unknown_name_raises(self, configs_dir: Path):
    with pytest.raises(ValueError, match='unknown secret'):
      credentials.build_scoped_store([TEST_SECRET, 'nonsense'])

  def test_absent_value_raises(self, configs_dir: Path):
    # strict: a declared name with no value on the host fails loudly here.
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    with pytest.raises(credentials.SecretNotFound):
      credentials.build_scoped_store([TEST_SECRET, 'brave'])

  def test_optional_present_is_hydrated(self, configs_dir: Path):
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    _write(configs_dir, 'openai.json', {'api_key': 'k'})
    store = credentials.build_scoped_store([TEST_SECRET], optional=['openai'])
    assert set(store) == {'test_secret.cred', 'openai.cred', credentials.REGISTRY_FILE}
    assert json.loads(store['openai.cred']) == {'api_key': 'k'}
    assert set(json.loads(store[credentials.REGISTRY_FILE])) == {TEST_SECRET, 'openai'}

  def test_optional_unresolvable_is_skipped(self, configs_dir: Path):
    # openai is a known registry secret but has no value on the host — best-effort,
    # so it is skipped rather than raising the way a required secret would.
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    store = credentials.build_scoped_store([TEST_SECRET], optional=['openai'])
    assert set(store) == {'test_secret.cred', credentials.REGISTRY_FILE}
    assert set(json.loads(store[credentials.REGISTRY_FILE])) == {TEST_SECRET}

  def test_optional_unknown_is_skipped(self, configs_dir: Path):
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    store = credentials.build_scoped_store([TEST_SECRET], optional=['nonsense'])
    assert set(store) == {'test_secret.cred', credentials.REGISTRY_FILE}

  def test_optional_also_required_hydrated_once(self, configs_dir: Path):
    # a name in both tiers resolves once via the strict required pass; the optional
    # pass skips it — required wins, never downgraded to best-effort. same name =
    # same instance, so the per-kind rule is not tripped either.
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    store = credentials.build_scoped_store([TEST_SECRET], optional=[TEST_SECRET])
    assert set(store) == {'test_secret.cred', credentials.REGISTRY_FILE}

  def test_variant_materializes_under_its_kind_name(self, configs_dir: Path, bro_dir: Path):
    # the scoped namespace is kinds-only: the registry entry and its cred file
    # are named by the kind, hydrated from the variant's sources
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    _write(bro_dir, 'github_token_alice', 'ghp_alice\n')
    store = credentials.build_scoped_store(['github+alice'])
    assert set(store) == {'github.cred', credentials.REGISTRY_FILE}
    assert store['github.cred'] == b'ghp_alice'
    registry = json.loads(store[credentials.REGISTRY_FILE])
    assert set(registry) == {'github'}
    assert registry['github']['sources'] == [{'file': 'github.cred'}]
    # the hook is re-rendered for the kind name — in-session `eval` pulls the
    # value via `credentials get github`, the name the scoped store resolves
    assert 'credentials get github' in registry['github']['install']
    assert 'github+alice' not in registry['github']['install']
    rebuilt = credentials._registry_from_dict(registry)
    assert rebuilt['github'].install == registry['github']['install']

  def test_optional_variant_also_materializes_under_its_kind_name(
    self, configs_dir: Path, bro_dir: Path
  ):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'openai+work': {'sources': [{'file': 'openai_work.json'}]}},
    )
    _write(bro_dir, 'openai_work.json', '{"api_key": "k"}')
    store = credentials.build_scoped_store([], optional=['openai+work'])
    assert set(store) == {'openai.cred', credentials.REGISTRY_FILE}
    assert set(json.loads(store[credentials.REGISTRY_FILE])) == {'openai'}

  def test_variant_of_hookless_kind_materializes_without_hook(
    self, configs_dir: Path, bro_dir: Path
  ):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'openai+work': {'sources': [{'file': 'openai_work.json'}]}},
    )
    _write(bro_dir, 'openai_work.json', '{"api_key": "k"}')
    store = credentials.build_scoped_store(['openai+work'])
    registry = json.loads(store[credentials.REGISTRY_FILE])
    assert registry['openai'] == {'sources': [{'file': 'openai.cred'}]}

  def test_two_instances_of_a_kind_raise(self, configs_dir: Path, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    _write(bro_dir, 'github_token_alice', 'ghp_alice')
    _write(configs_dir, 'cw_github_token_bro', 'ghp_bro')
    with pytest.raises(ValueError, match='installs at most one'):
      credentials.build_scoped_store(['github', 'github+alice'])

  def test_kind_conflict_across_tiers_raises(self, configs_dir: Path):
    # the check runs over the declared union up front — before resolution — so
    # it fires even though the optional variant is unknown and unresolvable
    _write(configs_dir, 'cw_github_token_bro', 'ghp_bro')
    with pytest.raises(ValueError, match='installs at most one'):
      credentials.build_scoped_store(['github'], optional=['github+alice'])


class TestScopedViewStore:
  def test_variant_reads_under_its_kind_name(self, configs_dir: Path, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'brog+alt': {'sources': [{'file': 'brog_alt.json'}]}},
    )
    _write(bro_dir, 'brog_alt.json', {'backend': 'alt'})
    _write(bro_dir, 'brog.json', {'backend': 'flow'})
    store = credentials.scoped_view_store(['brog+alt'])
    assert store.get_json('brog') == {'backend': 'alt'}
    assert store.known_names() == frozenset({'brog'})

  def test_bare_kind_reads_its_own_entry(self, configs_dir: Path, bro_dir: Path):
    _write(bro_dir, 'brog.json', {'backend': 'flow'})
    store = credentials.scoped_view_store(['brog'])
    assert store.get_json('brog') == {'backend': 'flow'}

  def test_nothing_is_fetched_at_construction(self, configs_dir: Path, bro_dir: Path, monkeypatch):
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    source = _TicketSource('ticket.json')
    registry = {'github': credentials.Secret('github', [source])}
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.scoped_view_store(['github'])
    assert source.mints == 0
    assert store.get('github') == 'ticket_1'
    assert source.mints == 1

  def test_bounded_to_the_scope(self, configs_dir: Path):
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    _write(configs_dir, 'brave.json', {'api_key': 'k'})
    store = credentials.scoped_view_store([TEST_SECRET])
    with pytest.raises(credentials.SecretNotFound):
      store.get('brave')

  def test_unresolvable_name_surfaces_at_read(self, configs_dir: Path):
    # lazy: the launch-time strictness of hydration is deliberately absent —
    # a value-less name builds fine and fails only when actually read
    store = credentials.scoped_view_store([TEST_SECRET])
    with pytest.raises(credentials.SecretNotFound):
      store.get(TEST_SECRET)

  def test_unknown_required_name_raises(self, configs_dir: Path):
    with pytest.raises(ValueError, match='unknown secret'):
      credentials.scoped_view_store(['nonsense'])

  def test_unknown_optional_name_is_skipped(self, configs_dir: Path):
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    store = credentials.scoped_view_store([TEST_SECRET], optional=['nonsense'])
    assert store.get_json(TEST_SECRET) == {'token': 't'}
    assert store.try_get('nonsense') is None

  def test_optional_names_join_the_view(self, configs_dir: Path):
    _write(configs_dir, 'test_secret.json', {'token': 't'})
    _write(configs_dir, 'openai.json', {'api_key': 'k'})
    store = credentials.scoped_view_store([TEST_SECRET], optional=['openai'])
    assert store.get_json('openai') == {'api_key': 'k'}

  def test_two_instances_of_a_kind_raise(self, configs_dir: Path, bro_dir: Path):
    _write(
      bro_dir,
      credentials.HOST_REGISTRY_FILE,
      {'github+alice': {'sources': [{'file': 'github_token_alice'}]}},
    )
    with pytest.raises(ValueError, match='installs at most one'):
      credentials.scoped_view_store(['github', 'github+alice'])

  def test_kind_reference_expands_through_the_selected_instance(
    self, configs_dir: Path, bro_dir: Path, monkeypatch
  ):
    # a `$cred` reference is spelled at kind level; the view resolves it through
    # the instance the scope selected, matching the session's own re-expansion
    _write(bro_dir, 'ticket.json', {'prefix': 'ticket'})
    _write(bro_dir, 'brog.secret', {'backend': 'github', 'token': {'$cred': 'github'}})
    registry = {
      'github+bot': credentials.Secret('github+bot', [_TicketSource('ticket.json')]),
      'brog': credentials.Secret('brog', [credentials.LocalSource('brog.secret')]),
    }
    monkeypatch.setattr(credentials, '_load_registry', lambda: registry)
    store = credentials.scoped_view_store(['brog', 'github+bot'])
    assert store.get_json('brog') == {'backend': 'github', 'token': 'ticket_1'}


class TestApplyGrantRevoke:
  def test_grant_adds(self):
    assert credentials.apply_grant_revoke({'a'}, grant=['b']) == {'a', 'b'}

  def test_revoke_removes(self):
    assert credentials.apply_grant_revoke({'a', 'b'}, revoke=['b']) == {'a'}

  def test_grant_and_revoke_combine(self):
    assert credentials.apply_grant_revoke({'a'}, grant=['b'], revoke=['a']) == {'b'}

  def test_empty_returns_copy(self):
    computed = {'a'}
    result = credentials.apply_grant_revoke(computed)
    assert result == {'a'}
    assert result is not computed  # never mutates the input

  def test_does_not_mutate_input(self):
    computed = {'a'}
    credentials.apply_grant_revoke(computed, grant=['b'], revoke=['a'])
    assert computed == {'a'}

  def test_grant_already_present_raises(self):
    with pytest.raises(ValueError, match='already in the set'):
      credentials.apply_grant_revoke({'a'}, grant=['a'])

  def test_revoke_absent_raises(self):
    with pytest.raises(ValueError, match='not in the set'):
      credentials.apply_grant_revoke({'a'}, revoke=['b'])

  def test_duplicate_grant_raises(self):
    # the second grant of the same name sees it already present
    with pytest.raises(ValueError, match='already in the set'):
      credentials.apply_grant_revoke({'a'}, grant=['b', 'b'])

  def test_duplicate_revoke_raises(self):
    with pytest.raises(ValueError, match='not in the set'):
      credentials.apply_grant_revoke({'a', 'b'}, revoke=['b', 'b'])

  def test_subject_names_the_set_in_errors(self):
    with pytest.raises(ValueError, match='already in the summon allow-list'):
      credentials.apply_grant_revoke({'a'}, grant=['a'], subject='summon allow-list')
    with pytest.raises(ValueError, match='not in the summon allow-list'):
      credentials.apply_grant_revoke({'a'}, revoke=['b'], subject='summon allow-list')

  def test_grant_and_revoke_same_name_raises(self):
    with pytest.raises(ValueError, match='grant and revoke the same'):
      credentials.apply_grant_revoke({'a'}, grant=['b'], revoke=['b'])


class TestInstallHooks:
  def test_framework_and_test_secrets_have_install_hooks(self):
    registry = credentials.default_registry()
    assert registry['github'].install is not None
    assert registry[TEST_SECRET].install is not None
    assert registry['openai'].install is None

  def test_claude_code_maps_to_token_file_with_install_hook(self):
    registry = credentials.default_registry()
    source = registry['claude_code'].sources[0]
    assert isinstance(source, credentials.LocalSource)
    assert source.file == 'claude_code_oauth_token'
    # install hook exports the env var claude reads above ~/.claude/.credentials.json
    assert registry['claude_code'].install is not None

  def test_test_secret_source_file(self):
    registry = credentials.default_registry()
    source = registry[TEST_SECRET].sources[0]
    assert isinstance(source, credentials.LocalSource)
    assert source.file == 'test_secret.json'

  def test_install_hooks_are_source_agnostic(self, configs_dir: Path):
    # hooks pull their value via `credentials get` at eval time — no resolved file
    # path is interpolated, so there's no quoting/injection surface. no files
    # written: presence is no longer a path check.
    out = credentials.install_hooks()
    # github → git credential helper + a PATH-front gh wrapper, each pulling the
    # token via `credentials get` at use time (fresh across minted app tokens);
    # no ambient GH_TOKEN export — the wrapper sets it per invocation
    assert 'credential.helper' in out
    assert 'credentials get github' in out
    assert '.local/bin/gh' in out
    assert 'GH_TOKEN' in out
    assert 'export GH_TOKEN' not in out
    assert 'TEST_SECRET' in out
    assert "credentials get 'test_secret'" in out
    # claude_code → exports CLAUDE_CODE_OAUTH_TOKEN via `credentials get`
    assert 'CLAUDE_CODE_OAUTH_TOKEN' in out
    assert "credentials get 'claude_code'" in out
    # every template directive is rendered away by emit time
    assert '{{' not in out
    # no absolute resolver path or source file is interpolated
    assert str(configs_dir) not in out
    assert 'test_secret.json' not in out
    assert 'openai' not in out

  def test_install_hooks_emit_for_all_declared_secrets(self, configs_dir: Path):
    # presence is no longer a path check: every registry secret that declares a
    # hook emits even with no local file present. in a scoped container the
    # registry *is* the hydrated (present) set, so this is the right bound.
    out = credentials.install_hooks()
    assert 'credentials get github' in out
    assert "credentials get 'test_secret'" in out

  def test_cli_install_hooks(self, configs_dir: Path, capsys):
    assert credentials.main(['credentials', 'install-hooks']) is None
    assert "credentials get 'test_secret'" in capsys.readouterr().out

  def test_cli_get_without_name_errors(self, configs_dir: Path, capsys):
    # the get subparser makes name a required positional, so argparse enforces it
    with pytest.raises(SystemExit):
      credentials.main(['credentials', 'get'])
    assert 'required: name' in capsys.readouterr().err


class TestWithoutBoto3:
  def test_imports_and_builds_sources_without_boto3(self):
    # only SSMSource.fetch may reach for boto3; simulate its absence in a fresh subprocess.
    import subprocess
    import sys

    code = (
      "import sys; sys.modules['boto3'] = None; "
      'from bro.base import credentials; '
      "credentials.Secret.from_dict('notion', "
      "{'sources': [{'type': 'ssm', 'parameter': '/p', 'region': 'eu-central-1'}]}); "
      "print('ok')"
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
