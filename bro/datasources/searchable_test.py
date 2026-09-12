from types import SimpleNamespace

import pytest

from bro.datasources import searchable
from bro.datasources.searchable import Hit, SearchableDataSource


class _FakeSource(SearchableDataSource):
  name = 'fake'
  summary = 'fake source'

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    return [Hit(id='1', title='t')]

  async def _fetch_content(self, id: str) -> str:
    return f'raw record for {id}'


def _boom(name: str) -> bool:
  raise AssertionError('availability must not be consulted on the no-query path')


@pytest.mark.asyncio
async def test_fetch_without_query_returns_raw_content(monkeypatch):
  # no query → raw record straight from `_fetch_content`, no summary, no cred read
  monkeypatch.setattr(searchable.credentials, 'available', _boom)
  assert await _FakeSource().fetch('x') == 'raw record for x'


@pytest.mark.asyncio
async def test_fetch_with_query_summarises_via_mu(monkeypatch):
  import bro.llm.mu as mu_module

  captured: list[str] = []

  async def fake_mu(prompt, result_cls, *contents, model=None, reasoning_effort=None):
    captured.append(prompt)
    return result_cls(summary='focused summary')

  monkeypatch.setattr(searchable.credentials, 'available', lambda name: True)
  monkeypatch.setattr(mu_module, 'mu', SimpleNamespace(aio=fake_mu))
  result = await _FakeSource().fetch('id-7', query='what is it?')
  assert result == 'focused summary'
  # the unified source_summary template carries source name, id, query, content
  assert 'what is it?' in captured[0]
  assert 'raw record for id-7' in captured[0]
  assert 'fake' in captured[0]
  assert 'id-7' in captured[0]


@pytest.mark.asyncio
async def test_fetch_with_query_raises_when_secret_absent(monkeypatch):
  # no raw-text fallback: the agent loop turns the raise into a tool result it
  # can retry with `query` omitted.
  monkeypatch.setattr(searchable.credentials, 'available', lambda name: False)
  with pytest.raises(ValueError, match='requires the `openai` secret'):
    await _FakeSource().fetch('x', query='q')


def test_bare_feature_name_is_one_universe_member(monkeypatch):
  class _FeatureSource(_FakeSource):
    summary = '{{when #features contains summary}}enabled{{end}}'

  monkeypatch.setattr(searchable.credentials, 'available', lambda name: True)
  assert _FeatureSource().rendered_summary() == 'enabled'


def test_one_shot_feature_declaration_is_reused(monkeypatch):
  class _OneShotFeatureSource(_FakeSource):
    summary = '{{when #features contains summary}}enabled{{end}}'
    feature_names = iter(('summary',))  # noqa: RUF012 — one-shot declaration

  monkeypatch.setattr(searchable.credentials, 'available', lambda name: True)
  source = _OneShotFeatureSource()
  assert source.rendered_summary() == 'enabled'
  assert source.rendered_summary() == 'enabled'


@pytest.mark.parametrize('feature_names', [7, ('summary', 7)])
def test_invalid_feature_names_raise(feature_names):
  class _InvalidFeatureSource(_FakeSource):
    pass

  _InvalidFeatureSource.feature_names = feature_names
  with pytest.raises(TypeError, match='_InvalidFeatureSource.feature_names'):
    _InvalidFeatureSource().text_variables()


def test_summary_secret_declared_optional():
  assert _FakeSource().optional_secrets == (searchable.SUMMARY_SECRET,)


def test_one_shot_secret_declarations_are_reused():
  class _OneShotSecretSource(_FakeSource):
    needed_secrets = iter(('brave',))  # noqa: RUF012 — one-shot declaration
    optional_secrets = iter(  # noqa: RUF012 — one-shot declaration
      (searchable.SUMMARY_SECRET,)
    )

  source = _OneShotSecretSource()
  first = source.as_mcp_server()
  second = source.as_mcp_server()
  assert first.needed_secrets == ('brave',)
  assert second.needed_secrets == ('brave',)
  assert first.optional_secrets == (searchable.SUMMARY_SECRET,)
  assert second.optional_secrets == (searchable.SUMMARY_SECRET,)


@pytest.mark.asyncio
async def test_as_mcp_server_normalizes_bare_secrets():
  class _KeyedSource(_FakeSource):
    name = 'keyed'
    needed_secrets = 'brave'

  server = _KeyedSource().as_mcp_server()
  assert server.needed_secrets == ('brave',)
  assert server.optional_secrets == (searchable.SUMMARY_SECRET,)
