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

  def test_get_parses_json_secret(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't', 'tasks_db_id': 'd'})
    store = self._store(
      credentials.Secret('notion', [credentials.LocalSource('notion.json')], text=False)
    )
    assert store.get('notion') == {'token': 't', 'tasks_db_id': 'd'}

  def test_get_strips_text_secret(self, configs_dir: Path):
    _write(configs_dir, 'cw_github_token', 'ghp_abc\n')
    store = self._store(
      credentials.Secret('github', [credentials.LocalSource('cw_github_token')], text=True)
    )
    assert store.get('github') == 'ghp_abc'

  def test_first_source_with_value_wins(self, configs_dir: Path):
    _write(configs_dir, 'second.json', {'who': 'second'})
    sources = [credentials.LocalSource('first.json'), credentials.LocalSource('second.json')]
    store = self._store(credentials.Secret('s', sources, text=False))
    assert store.get('s') == {'who': 'second'}

  def test_value_is_cached(self, configs_dir: Path):
    path = configs_dir / 'notion.json'
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(
      credentials.Secret('notion', [credentials.LocalSource('notion.json')], text=False)
    )
    assert store.get('notion') == {'token': 't'}
    path.unlink()
    assert store.get('notion') == {'token': 't'}

  def test_unknown_name_raises(self):
    with pytest.raises(credentials.SecretNotFound) as exc:
      credentials.Store({}).get('nope')
    assert exc.value.name == 'nope'
    assert exc.value.tried == []

  def test_no_source_has_value_raises_with_tried(self, configs_dir: Path):
    store = self._store(
      credentials.Secret('notion', [credentials.LocalSource('notion.json')], text=False)
    )
    with pytest.raises(credentials.SecretNotFound) as exc:
      store.get('notion')
    assert exc.value.tried == ['local:notion.json']

  def test_get_json_narrows_to_dict(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    store = self._store(
      credentials.Secret('notion', [credentials.LocalSource('notion.json')], text=False)
    )
    assert store.get_json('notion') == {'token': 't'}

  def test_get_json_rejects_text_secret(self, configs_dir: Path):
    _write(configs_dir, 'cw_github_token', 'ghp_abc')
    store = self._store(
      credentials.Secret('github', [credentials.LocalSource('cw_github_token')], text=True)
    )
    with pytest.raises(TypeError):
      store.get_json('github')

  def test_concurrent_get_fetches_once(self):
    # 8 threads resolve the same uncached secret at once. the lock makes
    # resolution single-flight, so the source is fetched exactly once; the
    # sleep widens the cache-miss window so a missing lock would duplicate it.
    calls: list[int] = []

    class _CountingSource:
      def fetch(self) -> str | None:
        calls.append(1)  # list.append is atomic under the GIL
        time.sleep(0.02)
        return '{"k": "v"}'

      def describe(self) -> str:
        return 'counting'

    store = self._store(credentials.Secret('s', [_CountingSource()], text=False))
    results: list[dict | str] = []

    def worker() -> None:
      results.append(store.get('s'))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert results == [{'k': 'v'}] * 8
    assert len(calls) == 1


class TestDefaultRegistry:
  def test_inventory_covers_known_secrets(self):
    registry = credentials.default_registry()
    for name in ('notion', 'focus', 'trails', 'openai', 'anthropic', 'tmdb', 'brave', 'github'):
      assert name in registry

  def test_github_tokens_are_text(self):
    registry = credentials.default_registry()
    assert registry['github'].text is True
    assert registry['notion'].text is False


class TestDefaultStore:
  def test_falls_back_to_builtin_registry(self, configs_dir: Path):
    _write(configs_dir, 'notion.json', {'token': 't'})
    assert credentials.default_store().get('notion') == {'token': 't'}

  def test_credentials_json_overrides_builtin(self, configs_dir: Path):
    _write(configs_dir, 'custom.json', {'token': 'x'})
    _write(
      configs_dir,
      credentials.REGISTRY_FILE,
      {'notion': {'sources': [{'type': 'local', 'file': 'custom.json'}], 'text': False}},
    )
    assert credentials.default_store().get('notion') == {'token': 'x'}


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
    _write(configs_dir, 'cw_github_token', 'ghp_abc\n')
    assert credentials.main(['credentials', 'get', 'github']) is None
    assert capsys.readouterr().out.strip() == 'ghp_abc'

  def test_missing_secret_exits_nonzero(self, configs_dir: Path, capsys):
    assert credentials.main(['credentials', 'get', 'notion']) == 1
    assert 'not found' in capsys.readouterr().err

  def test_field_on_text_secret_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'cw_github_token', 'ghp_abc')
    assert credentials.main(['credentials', 'get', 'github', '--field', 'api_key']) == 1
    assert 'scalar token' in capsys.readouterr().err

  def test_missing_field_exits_nonzero(self, configs_dir: Path, capsys):
    _write(configs_dir, 'anthropic.json', {'other': 'x'})
    assert credentials.main(['credentials', 'get', 'anthropic', '--field', 'api_key']) == 1
    assert 'no field' in capsys.readouterr().err
