from unittest.mock import patch

import pytest

from bro.datasources import wikipedia as wp


@pytest.mark.asyncio
async def test_search_parses_hits():
  async def fake_get_json(url, params):
    assert params['q'] == 'turing'
    assert params['limit'] == '3'
    return {
      'pages': [
        {'key': 'Alan_Turing', 'title': 'Alan Turing', 'description': 'British mathematician'},
        {'key': 'Turing_machine', 'title': 'Turing machine', 'description': None},
      ]
    }

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    hits = await wp.Wikipedia().search('turing', limit=3)
  assert len(hits) == 2
  assert hits[0].id == 'Alan_Turing'
  assert hits[0].title == 'Alan Turing'
  assert hits[0].snippet == 'British mathematician'
  assert hits[1].snippet is None


@pytest.mark.asyncio
async def test_fetch_content_returns_raw_extract():
  # query-focused summarisation lives in the SearchableDataSource base
  # (searchable_test.py); this verifies the source's raw extract retrieval.
  async def fake_get_json(url, params):
    return {
      'query': {
        'pages': {'42': {'title': 'Alan Turing', 'extract': 'Alan Turing was a mathematician.'}}
      }
    }

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    extract = await wp.Wikipedia()._fetch_content('Alan_Turing')
  assert extract == 'Alan Turing was a mathematician.'


@pytest.mark.asyncio
async def test_fetch_content_raises_on_missing_page():
  async def fake_get_json(url, params):
    return {'query': {'pages': {'-1': {'missing': ''}}}}

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    with pytest.raises(LookupError, match='does not exist'):
      await wp.Wikipedia()._fetch_content('Nonexistent_Page')


@pytest.mark.asyncio
async def test_search_uses_configured_language():
  captured_urls: list[str] = []

  async def fake_get_json(url, params):
    captured_urls.append(url)
    return {'pages': []}

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    await wp.Wikipedia(lang='ru').search('Москва')
  assert captured_urls[0].startswith('https://ru.wikipedia.org/')
