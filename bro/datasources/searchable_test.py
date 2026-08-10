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

  async def fake_mu(prompt, result_cls, *contents, reasoning_effort=None):
    captured.append(prompt)
    return result_cls(summary='focused summary')

  monkeypatch.setattr(searchable.credentials, 'available', lambda name: True)
  monkeypatch.setattr(mu_module, 'mu', fake_mu)
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


def test_summary_secret_declared_optional():
  assert searchable.SUMMARY_SECRET in _FakeSource().optional_secrets


@pytest.mark.asyncio
async def test_as_mcp_server_stamps_secrets():
  class _KeyedSource(_FakeSource):
    name = 'keyed'
    needed_secrets = ('brave',)

  server = _KeyedSource().as_mcp_server()
  assert set(server.needed_secrets) == {'brave'}
  assert searchable.SUMMARY_SECRET in server.optional_secrets
