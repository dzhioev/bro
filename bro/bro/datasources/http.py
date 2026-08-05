import urllib.parse
from typing import Optional

import aiohttp

# every fetch runs on a throwaway per-request ClientSession; aiohttp's default
# timeout (300 s total) would hang an agent tool call on a stuck upstream, so
# sessions get this tight explicit budget instead
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

_USER_AGENT = 'bro/1.0'


def _make_session(headers: Optional[dict[str, str]] = None) -> aiohttp.ClientSession:
  session_headers = {'User-Agent': _USER_AGENT}
  if headers is not None:
    session_headers.update(headers)
  return aiohttp.ClientSession(headers=session_headers, timeout=_TIMEOUT)


async def get_json(
  url: str, parameters: dict[str, str], headers: Optional[dict[str, str]] = None
) -> dict:
  full_url = f'{url}?{urllib.parse.urlencode(parameters)}' if len(parameters) > 0 else url
  async with _make_session(headers) as session:
    async with session.get(full_url) as response:
      response.raise_for_status()
      return await response.json()


async def get_text(url: str) -> str:
  async with _make_session() as session:
    async with session.get(url) as response:
      response.raise_for_status()
      return await response.text()
