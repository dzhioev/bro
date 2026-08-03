import importlib.metadata
from typing import Any, cast

import pytest

import brog.github
import brog.system
from base import credentials
from brog.system import build_system, default_system

_GITHUB_CONFIG = {'backend': 'github', 'token': 'gh-secret', 'repo': 'octo/scratch'}


def _external_backend(config_provider, config, author):
  return {'config_provider': config_provider, 'config': config, 'author': author}


def _entry_point(name: str, value: str) -> importlib.metadata.EntryPoint:
  return importlib.metadata.EntryPoint(name, value, brog.system._BACKEND_ENTRY_POINT_GROUP)


class TestBuildSystem:
  def test_github(self):
    system = build_system(lambda: _GITHUB_CONFIG)
    assert isinstance(system, brog.github.System)
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
    assert isinstance(system, brog.github.System)
    assert system._token() == 't2'

  def test_github_repo_derived_from_origin_when_omitted(self, monkeypatch):
    monkeypatch.setattr(brog.github, 'origin_repo', lambda: 'derived/name')
    system = build_system(lambda: {'backend': 'github', 'token': 'gh-secret'})
    assert isinstance(system, brog.github.System)
    assert system._repo == 'derived/name'

  def test_github_missing_token_rejected(self):
    with pytest.raises(ValueError, match="missing 'token'"):
      build_system(lambda: {'backend': 'github', 'repo': 'octo/scratch'})

  def test_contributed_backend_is_discovered(self, monkeypatch):
    monkeypatch.setattr(
      brog.system,
      '_backend_entry_points',
      lambda: (_entry_point('external', 'brog.system_test:_external_backend'),),
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
    assert brog.system._backend_entry_points() == ()
    assert calls == [{'group': 'bro.brog.backends'}]

  def test_absent_backend_has_a_clear_error(self, monkeypatch):
    monkeypatch.setattr(brog.system, '_backend_entry_points', lambda: ())
    with pytest.raises(ValueError, match="unknown brog backend 'flow'; known: github"):
      build_system(lambda: {'backend': 'flow'})

  def test_duplicate_contributed_backend_rejected(self, monkeypatch):
    monkeypatch.setattr(
      brog.system,
      '_backend_entry_points',
      lambda: (
        _entry_point('external', 'brog.system_test:_external_backend'),
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
      brog.system,
      '_backend_entry_points',
      lambda: (_entry_point('external', 'brog.system_test:_external_backend'),),
    )
    return requested

  def test_reads_the_brog_secret(self, backend, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    system = cast(Any, default_system())
    assert backend == ['brog']
    assert system['author'] is None

  def test_author_from_cw_bro(self, backend, monkeypatch):
    monkeypatch.setenv('CW_BRO', 'dev')
    assert cast(Any, default_system())['author'] == 'dev'

  def test_empty_cw_bro_means_no_author(self, backend, monkeypatch):
    monkeypatch.setenv('CW_BRO', '')
    assert cast(Any, default_system())['author'] is None
