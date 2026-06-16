import urllib.parse

import aiohttp
import trafilatura
from pydantic import BaseModel

from base import credentials, log
from bro.datasources.base import Hit, SearchableDataSource
from mu import Text, mu
from prompts import get_prompt

_SEARCH_URL = 'https://api.search.brave.com/res/v1/web/search'
_USER_AGENT = 'bro-librorian/1.0 (https://github.com/dzhioev/ppp)'
_MAX_EXTRACT_CHARS = 60_000


class _Summary(BaseModel):
  summary: str


class WebSearch(SearchableDataSource):
  name = 'web-search'
  needed_secrets = ('brave',)
  summary = (
    'Web search — open-ended search across the public web (Brave Search index). '
    'Use for finding canonical URLs, ids, or pages when a structured source has no '
    'direct entry. Search returns URLs; fetch downloads a URL and returns its main '
    'text (optionally summarised for the query).'
  )

  def __init__(self, store: credentials.Store | None = None):
    # lazy: defer the credential read so a Bro that declares WebSearch can still
    # be listed (`bro list`, `bro show`) when the key is not present
    self._store = store if store is not None else credentials.default_store()
    self._api_key: str | None = None

  def _resolve_api_key(self) -> str:
    if self._api_key is None:
      key: str = self._store.get_json('brave')['api_key']
      self._api_key = key
    return self._api_key

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    params = {'q': query, 'count': str(limit)}
    data = await _get_json(_SEARCH_URL, params, headers=self._auth_headers())
    results = data.get('web', {}).get('results', [])
    hits: list[Hit] = []
    for result in results[:limit]:
      url = result.get('url')
      if url is None or len(url) == 0:
        continue
      title = result.get('title') or url
      snippet = result.get('description')
      hits.append(Hit(id=url, title=title, snippet=snippet))
    return hits

  async def fetch(self, id: str, query: str | None = None) -> str:
    html = await _get_text(id)
    extracted = trafilatura.extract(html) or ''
    if len(extracted) == 0:
      raise LookupError(f'web-search: no extractable text at {id!r}')
    text = extracted[:_MAX_EXTRACT_CHARS]
    log.info(f'web-search: fetched {id!r} ({len(text):,} chars)')
    if query is None or len(query) == 0:
      return text
    prompt = get_prompt(
      'web_search_summary.prompt.template',
      query=query,
      url=id,
      text=text,
    )
    result = mu(prompt, _Summary, Text(text), reasoning_effort='low')
    return result.summary

  def _auth_headers(self) -> dict[str, str]:
    return {
      'X-Subscription-Token': self._resolve_api_key(),
      'Accept': 'application/json',
    }


async def _get_json(url: str, params: dict[str, str], headers: dict[str, str]) -> dict:
  full_url = f'{url}?{urllib.parse.urlencode(params)}'
  request_headers = {'User-Agent': _USER_AGENT, **headers}
  async with aiohttp.ClientSession(headers=request_headers) as session:
    async with session.get(full_url) as response:
      response.raise_for_status()
      return await response.json()


async def _get_text(url: str) -> str:
  async with aiohttp.ClientSession(headers={'User-Agent': _USER_AGENT}) as session:
    async with session.get(url) as response:
      response.raise_for_status()
      return await response.text()
