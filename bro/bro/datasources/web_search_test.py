import json
from pathlib import Path
from unittest.mock import patch

import pytest

from base import credentials
from bro.datasources import web_search as ws


@pytest.fixture
def brave_store(tmp_path: Path, monkeypatch) -> credentials.Store:
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path))
  (tmp_path / 'brave.json').write_text(json.dumps({'api_key': 'k'}))
  return credentials.Store(credentials.default_registry())


@pytest.mark.asyncio
async def test_search_parses_hits(brave_store):
  captured: dict = {}

  async def fake_get_json(url, params, headers):
    captured['url'] = url
    captured['params'] = params
    captured['headers'] = headers
    return {
      'web': {
        'results': [
          {
            'url': 'https://www.imdb.com/title/tt26581740/',
            'title': 'Weapons (2025) - IMDb',
            'description': '2025 horror film',
          },
          {
            'url': 'https://en.wikipedia.org/wiki/Weapons_(film)',
            'title': 'Weapons (film) - Wikipedia',
            'description': None,
          },
        ]
      }
    }

  with patch.object(ws, '_get_json', side_effect=fake_get_json):
    hits = await ws.WebSearch(store=brave_store).search('weapons 2025 horror', limit=2)
  assert captured['params'] == {'q': 'weapons 2025 horror', 'count': '2'}
  assert captured['headers']['X-Subscription-Token'] == 'k'
  assert len(hits) == 2
  assert hits[0].id == 'https://www.imdb.com/title/tt26581740/'
  assert hits[0].title == 'Weapons (2025) - IMDb'
  assert hits[0].snippet == '2025 horror film'
  assert hits[1].snippet is None


@pytest.mark.asyncio
async def test_search_respects_limit(brave_store):
  async def fake_get_json(url, params, headers):
    return {
      'web': {
        'results': [
          {'url': f'https://example.com/{i}', 'title': f't{i}', 'description': 'd'}
          for i in range(10)
        ]
      }
    }

  with patch.object(ws, '_get_json', side_effect=fake_get_json):
    hits = await ws.WebSearch(store=brave_store).search('q', limit=3)
  assert len(hits) == 3


@pytest.mark.asyncio
async def test_search_skips_results_without_url(brave_store):
  async def fake_get_json(url, params, headers):
    return {
      'web': {
        'results': [
          {'title': 'no url'},
          {'url': '', 'title': 'empty url'},
          {'url': 'https://example.com/ok', 'title': 'ok'},
        ]
      }
    }

  with patch.object(ws, '_get_json', side_effect=fake_get_json):
    hits = await ws.WebSearch(store=brave_store).search('q')
  assert len(hits) == 1
  assert hits[0].id == 'https://example.com/ok'


@pytest.mark.asyncio
async def test_fetch_without_query_returns_extracted_text(brave_store):
  async def fake_get_text(url):
    return '<html><body><article>Main article text.</article></body></html>'

  with (
    patch.object(ws, '_get_text', side_effect=fake_get_text),
    patch.object(ws.trafilatura, 'extract', return_value='Main article text.'),
  ):
    text = await ws.WebSearch(store=brave_store).fetch('https://example.com/x')
  assert text == 'Main article text.'


@pytest.mark.asyncio
async def test_fetch_with_query_summarises_via_mu(brave_store):
  captured_prompt: list[str] = []

  async def fake_get_text(url):
    return '<html><body>...</body></html>'

  def fake_mu(prompt, result_cls, *contents, reasoning_effort=None):
    captured_prompt.append(prompt)
    return result_cls(summary='focused summary about horror release')

  with (
    patch.object(ws, '_get_text', side_effect=fake_get_text),
    patch.object(ws.trafilatura, 'extract', return_value='Weapons released August 2025.'),
    patch.object(ws, 'mu', side_effect=fake_mu),
  ):
    result = await ws.WebSearch(store=brave_store).fetch(
      'https://www.imdb.com/title/tt26581740/',
      query='when did Weapons release?',
    )
  assert result == 'focused summary about horror release'
  assert 'when did Weapons release?' in captured_prompt[0]
  assert 'https://www.imdb.com/title/tt26581740/' in captured_prompt[0]
  assert 'Weapons released August 2025.' in captured_prompt[0]


@pytest.mark.asyncio
async def test_fetch_raises_when_extraction_empty(brave_store):
  async def fake_get_text(url):
    return '<html></html>'

  with (
    patch.object(ws, '_get_text', side_effect=fake_get_text),
    patch.object(ws.trafilatura, 'extract', return_value=''),
  ):
    with pytest.raises(LookupError, match='no extractable text'):
      await ws.WebSearch(store=brave_store).fetch('https://example.com/empty')


def test_api_key_loaded_lazily(tmp_path: Path, monkeypatch):
  # ctor should not read the credential — needed so `bro list` / `bro show` work
  # without the key present
  monkeypatch.setattr(credentials, 'CONFIGS_DIR', str(tmp_path))
  source = ws.WebSearch(store=credentials.Store(credentials.default_registry()))
  assert source._api_key is None
