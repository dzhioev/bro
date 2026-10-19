import importlib.metadata
import json
from typing import Any, cast

import pytest

import bro.brog.github as brog_github
import bro.brog.system as brog_system
from bro.base import credentials
from bro.brog.system import build_system, default_system

_GITHUB_CONFIG = {'backend': 'github', 'token': 'gh-secret', 'repo': 'octo/scratch'}


def _external_backend(config_provider, config, author):
  return {'config_provider': config_provider, 'config': config, 'author': author}


def _entry_point(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, brog_system._BACKEND_ENTRY_POINT_GROUP)


class TestBuildSystem:
  def test_github(self):
    system = build_system(lambda: _GITHUB_CONFIG)
    assert isinstance(system, brog_github.System)
    assert system._repo == 'octo/scratch'
    assert system._token() == 'gh-secret'

  def test_github_token_rereads_the_config_provider(self):
    configs = iter(
      [
        {'backend': 'github', 'token': 't1', 'repo': 'octo/scratch'},
        {'backend': 'github', 'token': 't2', 'repo': 'octo/scratch'},
      ]
    )
    system = build_system(lambda: next(configs))
    assert isinstance(system, brog_github.System)
    assert system._token() == 't2'

  def test_github_repo_derived_from_origin_when_omitted(self, monkeypatch):
    monkeypatch.setattr(brog_github, 'origin_repo', lambda: 'derived/name')
    system = build_system(lambda: {'backend': 'github', 'token': 'gh-secret'})
    assert isinstance(system, brog_github.System)
    assert system._repo == 'derived/name'

  def test_github_missing_token_rejected(self):
    with pytest.raises(ValueError, match="missing 'token'"):
      build_system(lambda: {'backend': 'github', 'repo': 'octo/scratch'})

  def test_contributed_backend_is_discovered(self, monkeypatch):
    monkeypatch.setattr(
      brog_system,
      '_backend_entry_points',
      lambda: (_entry_point('external', 'bro.brog.system_test:_external_backend'),),
    )
    config = {'backend': 'external', 'token': 't'}
    system = cast(Any, build_system(lambda: config, author='dev'))
    assert system['config'] == config
    assert system['author'] == 'dev'
    assert system['config_provider']() == config

  def test_backend_entry_points_use_the_expected_group(self, monkeypatch):
    calls = []

    def entry_points(**kwargs):
      calls.append(kwargs)
      return ()

    monkeypatch.setattr(importlib.metadata, 'entry_points', entry_points)
    assert brog_system._backend_entry_points() == ()
    assert calls == [{'group': 'bro.brog.backends'}]

  def test_absent_backend_has_a_clear_error(self, monkeypatch):
    monkeypatch.setattr(brog_system, '_backend_entry_points', lambda: ())
    with pytest.raises(ValueError, match="unknown brog backend 'flow'; known: github"):
      build_system(lambda: {'backend': 'flow'})

  def test_duplicate_contributed_backend_rejected(self, monkeypatch):
    monkeypatch.setattr(
      brog_system,
      '_backend_entry_points',
      lambda: (
        _entry_point('external', 'bro.brog.system_test:_external_backend'),
        _entry_point('external', 'other.module:factory'),
      ),
    )
    with pytest.raises(ValueError, match='duplicate brog backend'):
      build_system(lambda: {'backend': 'external'})

  def test_missing_backend_rejected(self):
    with pytest.raises(ValueError, match="missing 'backend'"):
      build_system(lambda: {'token': 't'})


class TestDefaultSystem:
  @pytest.fixture
  def backend(self, monkeypatch):
    requested: list[str] = []

    def fake_get_json(name: str) -> dict:
      requested.append(name)
      return {'backend': 'external'}

    monkeypatch.setattr(credentials, 'get_json', fake_get_json)
    monkeypatch.setattr(
      brog_system,
      '_backend_entry_points',
      lambda: (_entry_point('external', 'bro.brog.system_test:_external_backend'),),
    )
    return requested

  def test_reads_the_brog_secret(self, backend, monkeypatch):
    monkeypatch.delenv('RIDE_BRO', raising=False)
    system = cast(Any, default_system())
    assert backend == ['brog']
    assert system['author'] is None

  def test_author_from_ride_bro(self, backend, monkeypatch):
    monkeypatch.setenv('RIDE_BRO', 'dev')
    assert cast(Any, default_system())['author'] == 'dev'

  def test_empty_ride_bro_means_no_author(self, backend, monkeypatch):
    monkeypatch.setenv('RIDE_BRO', '')
    assert cast(Any, default_system())['author'] is None


def test_live_backend_and_repository_follow_the_credential_generation(tmp_path, monkeypatch):
  from types import SimpleNamespace

  import bro.brog.system as systems
  from bro.base.credential_apply import apply_request, atomic_file

  registry = credentials.default_registry()
  source = tmp_path / 'source'
  (source / 'creds').mkdir(parents=True)
  for instance, backend, repository in [
    ('one', 'github', 'owner/first'),
    ('two', 'other', 'owner/second'),
  ]:
    (source / 'creds' / ('brog+' + instance + '.cred')).write_text(
      json.dumps({'backend': backend, 'repo': repository})
    )
  directory = tmp_path / 'hooks'
  directed = tmp_path / 'session'
  files, kinds = credentials.build_scoped_store(
    credentials.Store(registry, source, {'brog': 'one'}), ['brog']
  )
  for filename, content in files.items():
    atomic_file(directed / filename, content)
  store = credentials.Store(registry, directed, {})
  credentials.install_hooks(registry, kinds, store, directory, {})
  builds = []

  def factory(name):
    def build(provider, config, author):
      builds.append(name)
      return SimpleNamespace(get_task=lambda task_id: (name, config['repo'], task_id))

    return build

  monkeypatch.setattr(systems, '_backend_factory', factory)
  with credentials.as_default_store(store):
    system = systems.current_system()
    assert system.get_task('first') == ('github', 'owner/first', 'first')
    assert system.get_task('again') == ('github', 'owner/first', 'again')
    files, _ = credentials.build_scoped_store(
      credentials.Store(registry, source, {'brog': 'two'}), ['brog']
    )
    import base64

    request = {
      'operation': 'brog-change',
      'changed': ['brog'],
      'files': {name: base64.b64encode(value).decode() for name, value in files.items()},
    }
    apply_request(directed, directory, request)
    assert system.get_task('second') == ('other', 'owner/second', 'second')
    assert builds == ['github', 'other']
    files, _ = credentials.build_scoped_store(credentials.Store(registry, source, {}), [])
    request = {
      'operation': 'brog-revoke',
      'changed': ['brog'],
      'files': {name: base64.b64encode(value).decode() for name, value in files.items()},
    }
    apply_request(directed, directory, request)
    with pytest.raises(credentials.SecretNotFound):
      system.get_task('revoked')
