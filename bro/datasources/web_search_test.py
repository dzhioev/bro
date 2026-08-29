import json
from pathlib import Path
from unittest.mock import patch

import pytest
import trafilatura

from bro.base import credentials
from bro.datasources import web_search as web_search


@pytest.fixture
def brave_store(tmp_path: Path) -> credentials.Store:
  material = tmp_path / credentials.MATERIAL_DIR / 'brave.cred'
  material.parent.mkdir()
  material.write_text(json.dumps({'api_key': 'k'}))
  return credentials.Store(credentials.default_registry(), tmp_path, {})


@pytest.mark.asyncio
async def test_search_parses_hits(brave_store):
  captured: dict = {}

  async def fake_get_json(url, parameters, headers):
    captured['url'] = url
    captured['parameters'] = parameters
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

  with patch.object(web_search, 'get_json', side_effect=fake_get_json):
    hits = await web_search.WebSearch(store=brave_store).search('weapons 2025 horror', limit=2)
  assert captured['parameters'] == {'q': 'weapons 2025 horror', 'count': '2'}
  assert captured['headers']['X-Subscription-Token'] == 'k'
  assert len(hits) == 2
  assert hits[0].id == 'https://www.imdb.com/title/tt26581740/'
  assert hits[0].title == 'Weapons (2025) - IMDb'
  assert hits[0].snippet == '2025 horror film'
  assert hits[1].snippet is None


@pytest.mark.asyncio
async def test_search_respects_limit(brave_store):
  async def fake_get_json(url, parameters, headers):
    return {
      'web': {
        'results': [
          {'url': f'https://example.com/{i}', 'title': f't{i}', 'description': 'd'}
          for i in range(10)
        ]
      }
    }

  with patch.object(web_search, 'get_json', side_effect=fake_get_json):
    hits = await web_search.WebSearch(store=brave_store).search('q', limit=3)
  assert len(hits) == 3


@pytest.mark.asyncio
async def test_search_skips_results_without_url(brave_store):
  async def fake_get_json(url, parameters, headers):
    return {
      'web': {
        'results': [
          {'title': 'no url'},
          {'url': '', 'title': 'empty url'},
          {'url': 'https://example.com/ok', 'title': 'ok'},
        ]
      }
    }

  with patch.object(web_search, 'get_json', side_effect=fake_get_json):
    hits = await web_search.WebSearch(store=brave_store).search('q')
  assert len(hits) == 1
  assert hits[0].id == 'https://example.com/ok'


@pytest.mark.asyncio
async def test_fetch_content_returns_extracted_text(brave_store):
  # query-focused summarisation lives in the SearchableDataSource base
  # (searchable_test.py); this verifies the source's raw record extraction.
  async def fake_get_text(url):
    return '<html><body><article>Main article text.</article></body></html>'

  with (
    patch.object(web_search, 'get_text', side_effect=fake_get_text),
    # the source imports trafilatura at fetch time, so patch the real module
    patch.object(trafilatura, 'extract', return_value='Main article text.'),
  ):
    text = await web_search.WebSearch(store=brave_store)._fetch_content('https://example.com/x')
  assert text == 'Main article text.'


@pytest.mark.asyncio
async def test_fetch_content_raises_when_extraction_empty(brave_store):
  async def fake_get_text(url):
    return '<html></html>'

  with (
    patch.object(web_search, 'get_text', side_effect=fake_get_text),
    patch.object(trafilatura, 'extract', return_value=''),
  ):
    with pytest.raises(LookupError, match='no extractable text'):
      await web_search.WebSearch(store=brave_store)._fetch_content('https://example.com/empty')


def test_api_key_loaded_lazily(tmp_path: Path):
  # ctor should not read the credential — needed so `bro list` / `bro show` work
  # without the key present
  source = web_search.WebSearch(
    store=credentials.Store(credentials.default_registry(), tmp_path, {})
  )
  assert source._api_key is None
