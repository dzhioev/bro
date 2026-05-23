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
async def test_fetch_without_query_returns_raw_extract():
  async def fake_get_json(url, params):
    return {
      'query': {
        'pages': {'42': {'title': 'Alan Turing', 'extract': 'Alan Turing was a mathematician.'}}
      }
    }

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    extract = await wp.Wikipedia().fetch('Alan_Turing')
  assert extract == 'Alan Turing was a mathematician.'


@pytest.mark.asyncio
async def test_fetch_with_query_summarises_via_mu():
  async def fake_get_json(url, params):
    return {
      'query': {
        'pages': {
          '42': {
            'title': 'Alan Turing',
            'extract': 'Alan Turing was a British mathematician and codebreaker.',
          }
        }
      }
    }

  captured_prompt: list[str] = []

  def fake_mu(prompt, result_cls, *contents, reasoning_effort=None):
    captured_prompt.append(prompt)
    return result_cls(summary='focused summary about codebreaking')

  with (
    patch.object(wp, '_get_json', side_effect=fake_get_json),
    patch.object(wp, 'mu', side_effect=fake_mu),
  ):
    result = await wp.Wikipedia().fetch('Alan_Turing', query='what did he do at Bletchley?')
  assert result == 'focused summary about codebreaking'
  assert 'what did he do at Bletchley?' in captured_prompt[0]
  assert 'Alan Turing' in captured_prompt[0]


@pytest.mark.asyncio
async def test_fetch_raises_on_missing_page():
  async def fake_get_json(url, params):
    return {'query': {'pages': {'-1': {'missing': ''}}}}

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    with pytest.raises(LookupError, match='does not exist'):
      await wp.Wikipedia().fetch('Nonexistent_Page')


@pytest.mark.asyncio
async def test_search_uses_configured_language():
  captured_urls: list[str] = []

  async def fake_get_json(url, params):
    captured_urls.append(url)
    return {'pages': []}

  with patch.object(wp, '_get_json', side_effect=fake_get_json):
    await wp.Wikipedia(lang='ru').search('Москва')
  assert captured_urls[0].startswith('https://ru.wikipedia.org/')
