import json
import threading
import time
from pathlib import Path

import pytest

from base import credentials


@pytest.fixture
def configs_dir(monkeypatch, tmp_path: Path) -> Path:
  """point the resolver's project search root at a tmp dir, isolate the `~/.ppp`
  fallback to an empty tmp dir (so the host's real store can't leak in), and reset
  the singleton."""
  configs = tmp_path / 'configs'
  ppp = tmp_path / 'ppp'
  configs.mkdir()
  ppp.mkdir()
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(configs))
  monkeypatch.setattr(credentials, 'PPP_DIR', str(ppp))
  monkeypatch.setattr(credentials, '_default_store', None)
  return configs


@pytest.fixture
def ppp_dir(configs_dir: Path) -> Path:
  """the `~/.ppp` fallback search root set up alongside `configs_dir`."""
  return configs_dir.parent / 'ppp'


def _write(dir: Path, name: str, payload) -> None:
  (dir / name).write_text(payload if isinstance(payload, str) else json.dumps(payload))


class TestLocalSource:
  def test_fetch_reads_from_configs_dir(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "t"}'

  def test_fetch_falls_back_to_ppp_dir(self, configs_dir: Path, ppp_dir: Path):
    _write(ppp_dir, 'notion.json', {'token': 'p'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "p"}'

  def test_configs_dir_wins_over_ppp_dir(self, configs_dir: Path, ppp_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 'c'})
    _write(ppp_dir, 'notion.json', {'token': 'p'})
    assert credentials.LocalSource('notion.json').fetch() == '{"token": "c"}'

  def test_fetch_returns_none_when_absent(self, configs_dir: Path):
    assert credentials.LocalSource('missing.json').fetch() is None

  def test_describe(self):
    assert credentials.LocalSource('notion.json').describe() == 'local:notion.json'


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

  def test_unknown_name_raises(self):
    with pytest.raises(credentials.SecretNotFound) as exc:
      credentials.Store({}).get('nope')
    assert exc.value.name == 'nope'
    assert exc.value.tried == []

  def test_no_source_has_value_raises_with_tried(self, configs_dir: Path):
    store = self._store(credentials.Secret('notion', [credentials.LocalSource('notion.json')]))
    with pytest.raises(credentials.SecretNotFound) as exc:
      store.get('notion')
    assert exc.value.tried == ['local:notion.json']

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
      def fetch(self) -> str | None:
        calls.append(1)  # list.append is atomic under the GIL
        time.sleep(0.02)
        return 'v'

      def describe(self) -> str:
        return 'counting'

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


class TestDefaultRegistry:
  def test_inventory_covers_known_secrets(self):
    registry = credentials.default_registry()
    for name in ('notion', 'focus', 'trails', 'openai', 'anthropic', 'tmdb', 'brave', 'github'):
      assert name in registry

  def test_github_maps_to_bro_token_file(self):
    # one `github` entry resolves the token bro containers push with; there is no
    # separate human-PAT entry (it is unused by any reader).
    registry = credentials.default_registry()
    source = registry['github'].sources[0]
    assert isinstance(source, credentials.LocalSource)
    assert source.file == 'cw_github_token_bro'
    assert 'github_bro' not in registry


class TestDefaultStore:
  def test_falls_back_to_builtin_registry(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    assert credentials.default_store().get_json('notion') == {'token': 't'}

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

  def test_credentials_json_in_ppp_dir_overrides_builtin(self, configs_dir: Path, ppp_dir: Path):
    # a scoped credentials.json mounted at the container's ~/.ppp takes effect:
    # the registry load searches that dir too (no file in <project>/.configs there).
    _write(ppp_dir, 'custom.json', {'token': 'p'})
    _write(
      ppp_dir,
      credentials.REGISTRY_FILE,
      {'notion': {'sources': [{'type': 'local', 'file': 'custom.json'}]}},
    )
    registry = credentials._load_registry()
    assert set(registry) == {'notion'}
    assert credentials.default_store().get_json('notion') == {'token': 'p'}


class TestCli:
  def test_get_json_prints_json(self, configs_dir: Path, capsys):
    _write(configs_dir, 'notion.json', {'token': 't'})
    assert credentials.main(['credentials', 'get', 'notion']) is None
    assert json.loads(capsys.readouterr().out) == {'token': 't'}

  def test_get_field_prints_value(self, configs_dir: Path, capsys):
    _write(configs_dir, 'anthropic.json', {'api_key': 'sk-xyz'})
    assert credentials.main(['credentials', 'get', 'anthropic', '--field', 'api_key']) is None
    assert capsys.readouterr().out.strip() == 'sk-xyz'

  def test_get_text_prints_string(self, configs_dir: Path, capsys):
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc\n')
    assert credentials.main(['credentials', 'get', 'github']) is None
    assert capsys.readouterr().out.strip() == 'ghp_abc'

  def test_missing_secret_exits_nonzero(self, configs_dir: Path, capsys):
    assert credentials.main(['credentials', 'get', 'notion']) == 1
    assert 'not found' in capsys.readouterr().err

  def test_field_on_non_json_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc')
    assert credentials.main(['credentials', 'get', 'github', '--field', 'api_key']) == 1
    assert 'not valid json' in capsys.readouterr().err

  def test_missing_field_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'anthropic.json', {'other': 'x'})
    assert credentials.main(['credentials', 'get', 'anthropic', '--field', 'api_key']) == 1
    assert 'no field' in capsys.readouterr().err

  def test_json_flag_pretty_prints(self, configs_dir: Path, capsys):
    _write(configs_dir, 'notion.json', {'token': 't', 'db': 'd'})
    assert credentials.main(['credentials', 'get', 'notion', '--json']) is None
    out = capsys.readouterr().out
    assert json.loads(out) == {'token': 't', 'db': 'd'}
    assert '\n  ' in out  # indent=2

  def test_json_flag_on_non_json_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc')
    assert credentials.main(['credentials', 'get', 'github', '--json']) == 1
    assert 'not valid json' in capsys.readouterr().err


class TestBuildScopedStore:
  def test_builds_files_and_scoped_registry(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc\n')
    store = credentials.build_scoped_store(['notion', 'github'])
    # one entry per secret keyed by its file name, plus the scoped registry
    assert set(store) == {'notion.json', 'cw_github_token_bro', credentials.REGISTRY_FILE}
    # each secret's raw text (stripped) as bytes
    assert json.loads(store['notion.json']) == {'token': 't'}
    assert store['cw_github_token_bro'] == b'ghp_abc'
    registry = json.loads(store[credentials.REGISTRY_FILE])
    assert set(registry) == {'notion', 'github'}
    # the install hook rides along so the container can apply it generically;
    # the source omits `type` (local is the default)
    assert registry['github']['sources'] == [{'file': 'cw_github_token_bro'}]
    assert 'install' in registry['github']
    # a secret with no install hook carries none
    assert 'install' not in registry['notion']

  def test_empty_names_yields_only_registry(self, configs_dir: Path):
    # cw always cps a store in (even a zero-secret session), so the registry
    # file is always present — an empty bounding registry.
    store = credentials.build_scoped_store([])
    assert set(store) == {credentials.REGISTRY_FILE}
    assert json.loads(store[credentials.REGISTRY_FILE]) == {}

  def test_scoped_store_bounds_container_registry(
    self, configs_dir: Path, monkeypatch, tmp_path: Path
  ):
    # materialising the store as a container's ~/.ppp bounds it to the built
    # set: a non-declared secret resolves to a clean SecretNotFound.
    _write(configs_dir, 'notion.json', {'token': 't'})
    _write(configs_dir, 'tmdb.json', {'api_key': 'k'})
    dest = tmp_path / 'scoped'
    dest.mkdir()
    for fname, data in credentials.build_scoped_store(['notion']).items():
      (dest / fname).write_bytes(data)
    # resolve as the container would: scoped dir is its ~/.ppp (and there is no
    # <project>/.configs). the scoped credentials.json bounds the registry.
    monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path / 'absent'))
    monkeypatch.setattr(credentials, 'PPP_DIR', str(dest))
    monkeypatch.setattr(credentials, '_default_store', None)
    assert credentials.default_store().get_json('notion') == {'token': 't'}
    with pytest.raises(credentials.SecretNotFound):
      credentials.default_store().get('tmdb')

  def test_unknown_name_raises(self, configs_dir: Path):
    with pytest.raises(ValueError, match='unknown secret'):
      credentials.build_scoped_store(['notion', 'nonsense'])

  def test_absent_value_raises(self, configs_dir: Path):
    # strict: a declared name with no value on the host fails loudly here.
    _write(configs_dir, 'notion.json', {'token': 't'})
    with pytest.raises(credentials.SecretNotFound):
      credentials.build_scoped_store(['notion', 'tmdb'])


class TestInstallHooks:
  def test_github_and_aws_have_install_hooks(self):
    registry = credentials.default_registry()
    assert registry['github'].install is not None
    assert registry['aws'].install is not None
    assert registry['notion'].install is None

  def test_aws_source_file(self):
    registry = credentials.default_registry()
    source = registry['aws'].sources[0]
    assert isinstance(source, credentials.LocalSource)
    assert source.file == 'aws_credentials'

  def test_install_hooks_emits_for_present_secrets(self, configs_dir: Path):
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc')
    _write(configs_dir, 'aws_credentials', '[default]\naws_access_key_id=AKIA\n')
    _write(configs_dir, 'notion.json', {'token': 't'})
    out = credentials.install_hooks()
    # github → git credential helper + GH_TOKEN; aws → AWS_SHARED_CREDENTIALS_FILE
    assert 'credential.helper' in out
    assert 'GH_TOKEN' in out
    assert 'AWS_SHARED_CREDENTIALS_FILE=' in out
    # {path} resolved to the actual file path; notion declares no hook
    assert str(configs_dir / 'cw_github_token_bro') in out
    assert 'notion' not in out

  def test_install_hooks_skips_absent_secrets(self, configs_dir: Path):
    # github file present, aws absent → only github's hook emits
    _write(configs_dir, 'cw_github_token_bro', 'ghp_abc')
    out = credentials.install_hooks()
    assert 'GH_TOKEN' in out
    assert 'AWS_SHARED_CREDENTIALS_FILE' not in out

  def test_cli_install_hooks(self, configs_dir: Path, capsys):
    _write(configs_dir, 'aws_credentials', '[default]\n')
    assert credentials.main(['credentials', 'install-hooks']) is None
    assert 'AWS_SHARED_CREDENTIALS_FILE=' in capsys.readouterr().out

  def test_cli_get_without_name_errors(self, configs_dir: Path, capsys):
    assert credentials.main(['credentials', 'get']) == 1
    assert 'requires a secret name' in capsys.readouterr().err
