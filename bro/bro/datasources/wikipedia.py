import urllib.parse

import aiohttp

from base import log
from bro.datasources.searchable import Hit, SearchableDataSource

_SEARCH_URL = 'https://{language}.wikipedia.org/w/rest.php/v1/search/page'
_EXTRACT_URL = 'https://{language}.wikipedia.org/w/api.php'
_USER_AGENT = 'bro-librorian/1.0 (https://github.com/dzhioev/ppp)'


class Wikipedia(SearchableDataSource):
  name = 'wikipedia'
  summary = (
    'Wikipedia articles — general-knowledge reference covering people, places, events, '
    'organisations, science, and culture. Use for established facts; not for breaking news.'
  )

  def __init__(self, language: str = 'en'):
    self._language = language

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    url = _SEARCH_URL.format(language=self._language)
    parameters = {'q': query, 'limit': str(limit)}
    data = await _get_json(url, parameters)
    pages = data.get('pages', [])
    return [
      Hit(
        id=page['key'],
        title=page['title'],
        snippet=page.get('description'),
      )
      for page in pages
    ]

  async def _fetch_content(self, id: str) -> str:
    title, extract = await self._fetch_extract(id)
    log.info(f'wikipedia: fetched {title!r} ({len(extract):,} chars)')
    return extract

  async def _fetch_extract(self, id: str) -> tuple[str, str]:
    url = _EXTRACT_URL.format(language=self._language)
    parameters = {
      'action': 'query',
      'format': 'json',
      'prop': 'extracts',
      'explaintext': '1',
      'redirects': '1',
      'titles': id.replace('_', ' '),
    }
    data = await _get_json(url, parameters)
    pages = data.get('query', {}).get('pages', {})
    if len(pages) == 0:
      raise LookupError(f'wikipedia: no page for id {id!r}')
    page = next(iter(pages.values()))
    if 'missing' in page:
      raise LookupError(f'wikipedia: page {id!r} does not exist')
    title = page.get('title', id)
    extract = page.get('extract', '')
    return title, extract


async def _get_json(url: str, parameters: dict[str, str]) -> dict:
  full_url = f'{url}?{urllib.parse.urlencode(parameters)}'
  async with aiohttp.ClientSession(headers={'User-Agent': _USER_AGENT}) as session:
    async with session.get(full_url) as response:
      response.raise_for_status()
      return await response.json()
