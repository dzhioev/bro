import urllib.parse

import aiohttp
from pydantic import BaseModel

from base import log
from bro.datasources.base import Hit, SearchableDataSource
from mu import Text, mu
from prompts import get_prompt

_SEARCH_URL = 'https://{lang}.wikipedia.org/w/rest.php/v1/search/page'
_EXTRACT_URL = 'https://{lang}.wikipedia.org/w/api.php'
_USER_AGENT = 'bro-librorian/1.0 (https://github.com/dzhioev/ppp)'


class _Summary(BaseModel):
  summary: str


class Wikipedia(SearchableDataSource):
  name = 'wikipedia'
  summary = (
    'Wikipedia articles — general-knowledge reference covering people, places, events, '
    'organisations, science, and culture. Use for established facts; not for breaking news.'
  )

  def __init__(self, lang: str = 'en'):
    self._lang = lang

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    url = _SEARCH_URL.format(lang=self._lang)
    params = {'q': query, 'limit': str(limit)}
    data = await _get_json(url, params)
    pages = data.get('pages', [])
    return [
      Hit(
        id=page['key'],
        title=page['title'],
        snippet=page.get('description'),
      )
      for page in pages
    ]

  async def fetch(self, id: str, query: str | None = None) -> str:
    title, extract = await self._fetch_extract(id)
    log.info(f'wikipedia: fetched {title!r} ({len(extract):,} chars)')
    if query is None or len(query) == 0:
      return extract
    prompt = get_prompt(
      'wikipedia_summary.prompt.template',
      query=query,
      title=title,
      extract=extract,
    )
    result = mu(prompt, _Summary, Text(extract), reasoning_effort='low')
    return result.summary

  async def _fetch_extract(self, id: str) -> tuple[str, str]:
    url = _EXTRACT_URL.format(lang=self._lang)
    params = {
      'action': 'query',
      'format': 'json',
      'prop': 'extracts',
      'explaintext': '1',
      'redirects': '1',
      'titles': id.replace('_', ' '),
    }
    data = await _get_json(url, params)
    pages = data.get('query', {}).get('pages', {})
    if len(pages) == 0:
      raise LookupError(f'wikipedia: no page for id {id!r}')
    page = next(iter(pages.values()))
    if 'missing' in page:
      raise LookupError(f'wikipedia: page {id!r} does not exist')
    title = page.get('title', id)
    extract = page.get('extract', '')
    return title, extract


async def _get_json(url: str, params: dict[str, str]) -> dict:
  full_url = f'{url}?{urllib.parse.urlencode(params)}'
  async with aiohttp.ClientSession(headers={'User-Agent': _USER_AGENT}) as session:
    async with session.get(full_url) as response:
      response.raise_for_status()
      return await response.json()
