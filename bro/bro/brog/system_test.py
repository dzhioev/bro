import pytest

import brog.flow_proxy
import brog.github
from base import credentials
from brog.system import build_system, default_system

_HTTP_CONFIG = {
  'backend': 'flow',
  'transport': 'http',
  'url': 'https://flow.example.com',
  'token': 'bearer-secret',
}

# the notion.json shape; NotionAPI construction is offline, so building the local
# transport from it needs no network
_NOTION_CONFIG = {
  'token': 'notion-secret',
  'tasks_db_id': 'db-tasks',
  'events_db_id': 'db-events',
  'projects_db_id': 'db-projects',
  'media_db_id': 'db-media',
}

_LOCAL_CONFIG = {'backend': 'flow', 'transport': 'local', 'notion': _NOTION_CONFIG}

_GITHUB_CONFIG = {'backend': 'github', 'token': 'gh-secret', 'repo': 'octo/scratch'}


class TestBuildSystem:
  def test_flow_http(self):
    system = build_system(lambda: _HTTP_CONFIG)
    assert isinstance(system, brog.flow_proxy.System)
    assert isinstance(system._transport, brog.flow_proxy.HTTPTransport)

  def test_flow_local(self):
    system = build_system(lambda: _LOCAL_CONFIG)
    assert isinstance(system, brog.flow_proxy.System)
    assert isinstance(system._transport, brog.flow_proxy.LocalTransport)

  def test_author_threads_through(self):
    system = build_system(lambda: _HTTP_CONFIG, author='ppp-dev')
    assert isinstance(system, brog.flow_proxy.System)
    assert system._author == 'ppp-dev'

  def test_author_defaults_to_none(self):
    system = build_system(lambda: _HTTP_CONFIG)
    assert isinstance(system, brog.flow_proxy.System)
    assert system._author is None

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
    # the build consumed the first config; each token read re-consults the provider
    assert system._token() == 't2'

  def test_github_repo_derived_from_origin_when_omitted(self, monkeypatch):
    monkeypatch.setattr(brog.github, 'origin_repo', lambda: 'derived/name')
    system = build_system(lambda: {'backend': 'github', 'token': 'gh-secret'})
    assert isinstance(system, brog.github.System)
    assert system._repo == 'derived/name'

  def test_github_missing_token_rejected(self):
    with pytest.raises(ValueError, match="missing 'token'"):
      build_system(lambda: {'backend': 'github', 'repo': 'octo/scratch'})

  def test_unknown_backend_rejected(self):
    with pytest.raises(ValueError, match="unknown brog backend 'jira'"):
      build_system(lambda: {'backend': 'jira', 'token': 't'})

  def test_missing_backend_rejected(self):
    with pytest.raises(ValueError, match="missing 'backend'"):
      build_system(lambda: {'transport': 'http'})

  def test_unknown_transport_rejected(self):
    with pytest.raises(ValueError, match="unknown brog flow transport 'stdio'"):
      build_system(lambda: {'backend': 'flow', 'transport': 'stdio'})

  def test_missing_transport_rejected(self):
    with pytest.raises(ValueError, match="missing 'transport'"):
      build_system(lambda: {'backend': 'flow'})

  @pytest.mark.parametrize('key', ['url', 'token'])
  def test_http_missing_key_rejected(self, key):
    config = {k: v for k, v in _HTTP_CONFIG.items() if k != key}
    with pytest.raises(ValueError, match=f"missing '{key}'"):
      build_system(lambda: config)

  def test_local_missing_notion_rejected(self):
    with pytest.raises(ValueError, match="missing 'notion'"):
      build_system(lambda: {'backend': 'flow', 'transport': 'local'})

  def test_local_non_object_notion_rejected(self):
    with pytest.raises(ValueError, match='"notion" must be an object'):
      build_system(lambda: {'backend': 'flow', 'transport': 'local', 'notion': 'nope'})


class TestDefaultSystem:
  @pytest.fixture
  def brog_config(self, monkeypatch):
    requested: list[str] = []

    def fake_get_json(name: str) -> dict:
      requested.append(name)
      return dict(_HTTP_CONFIG)

    monkeypatch.setattr(credentials, 'get_json', fake_get_json)
    return requested

  def test_reads_the_brog_secret(self, brog_config, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    system = default_system()
    assert brog_config == ['brog']
    assert isinstance(system, brog.flow_proxy.System)

  def test_author_from_cw_bro(self, brog_config, monkeypatch):
    monkeypatch.setenv('CW_BRO', 'ppp-dev')
    system = default_system()
    assert isinstance(system, brog.flow_proxy.System)
    assert system._author == 'ppp-dev'

  def test_no_persona_means_no_author(self, brog_config, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    system = default_system()
    assert isinstance(system, brog.flow_proxy.System)
    assert system._author is None

  def test_empty_cw_bro_means_no_author(self, brog_config, monkeypatch):
    monkeypatch.setenv('CW_BRO', '')
    system = default_system()
    assert isinstance(system, brog.flow_proxy.System)
    assert system._author is None
