from typing import Optional

from bro.base import credentials, log
from bro.datasources.http import get_json, get_text
from bro.datasources.searchable import Hit, SearchableDataSource

_SEARCH_URL = 'https://api.search.brave.com/res/v1/web/search'
_MAX_EXTRACT_CHARS = 60_000


class WebSearch(SearchableDataSource):
  name = 'web-search'
  needed_secrets = ('brave',)
  summary = (
    'Web search — open-ended search across the public web (Brave Search index). '
    'Use for finding canonical URLs, ids, or pages when a structured source has no '
    'direct entry. Search returns URLs; fetch downloads a URL and returns its main '
    'text'
    '{{iff #features contains summary}}, summarized for your query when you pass one{{else}} '
    '(raw text; the `query` parameter is unavailable this session){{end}}.'
  )

  def __init__(self, store: Optional[credentials.Store] = None):
    # lazy: defer the credential read so a Bro that declares WebSearch can still
    # be listed (`bro list`, `bro show`) when the key is not present
    self._store = store if store is not None else credentials.default_store()
    self._api_key: Optional[str] = None

  def _resolve_api_key(self) -> str:
    if self._api_key is None:
      key: str = self._store.get_json('brave')['api_key']
      self._api_key = key
    return self._api_key

  async def search(self, query: str, limit: int = 5) -> list[Hit]:
    parameters = {'q': query, 'count': str(limit)}
    data = await get_json(_SEARCH_URL, parameters, headers=self._auth_headers())
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

  async def _fetch_content(self, id: str) -> str:
    # deferred: trafilatura costs ~1s to import, and every bro declaring this
    # source pays the module import at construction — only fetch needs it
    import trafilatura

    html = await get_text(id)
    raw_extracted = trafilatura.extract(html)
    extracted = raw_extracted if raw_extracted is not None else ''
    if len(extracted) == 0:
      raise LookupError(f'web-search: no extractable text at {id!r}')
    text = extracted[:_MAX_EXTRACT_CHARS]
    log.info(f'web-search: fetched {id!r} ({len(text):,} chars)')
    return text

  def _auth_headers(self) -> dict[str, str]:
    return {
      'X-Subscription-Token': self._resolve_api_key(),
      'Accept': 'application/json',
    }
