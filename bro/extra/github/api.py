"""GitHub REST client: JSON verbs over stdlib urllib with a transient-retry policy."""

import http.client
import json
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from bro.base import log

# transient HTTP statuses worth retrying: server errors, rate limiting, and
# 401/403 — GitHub returns these for token-propagation and secondary-rate-limit
# blips that clear within seconds (observed on PR #169: a lone 401 then 200).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_AUTH_STATUSES = frozenset({401, 403})
_MAX_ATTEMPTS = 5
_BASE_BACKOFF = 1.0  # seconds; doubled per attempt
_MAX_BACKOFF = 30.0  # ceiling for both exponential backoff and server-hinted waits


def is_transient(error: urllib.error.URLError) -> bool:
  """whether a failed GitHub call is a blip worth retrying vs a genuine error.

  HTTPError is a subclass of URLError; a bare URLError is a network/transport
  failure, which is always transient. genuine client errors (404, malformed
  request) surface immediately.
  """
  if isinstance(error, urllib.error.HTTPError):
    return error.code in _RETRYABLE_STATUSES or error.code in _TRANSIENT_AUTH_STATUSES
  return True


def _retry_delay(error: urllib.error.URLError, attempt: int) -> float:
  """seconds to wait before the next attempt (0-indexed), honoring server hints.

  prefers the server's own `Retry-After` (secondary rate limits) or
  `X-RateLimit-Reset` (when the remaining quota is exhausted); falls back to
  exponential backoff. all waits are capped at `_MAX_BACKOFF`.
  """
  backoff = min(_MAX_BACKOFF, _BASE_BACKOFF * (2**attempt))
  if not isinstance(error, urllib.error.HTTPError):
    return backoff
  headers = error.headers
  retry_after = headers.get('Retry-After')
  if retry_after is not None:
    hinted = _parse_retry_after(retry_after)
    if hinted is not None:
      return min(_MAX_BACKOFF, hinted)
  reset = headers.get('X-RateLimit-Reset')
  if reset is not None and headers.get('X-RateLimit-Remaining') == '0':
    try:
      return min(_MAX_BACKOFF, max(0.0, float(reset) - time.time()))
    except ValueError:
      pass
  return backoff


def _parse_retry_after(value: str) -> Optional[float]:
  """parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
  try:
    return float(value)
  except ValueError:
    pass
  try:
    return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
  except (TypeError, ValueError):
    return None


def _request(method: str, url: str, token: str, body: Optional[Any] = None) -> Any:
  """one authenticated call, retried per the transient policy; parsed JSON response
  (None when the response has no body, e.g. a 204).

  the retry policy applies to mutating verbs too — a retried write can rarely land
  twice (the write succeeded, the response was lost); accepted at this scale.
  """
  headers = {
    'Authorization': f'Bearer {token}',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  }
  data = None
  if body is not None:
    data = json.dumps(body).encode()
    headers['Content-Type'] = 'application/json'
  prepared = urllib.request.Request(url, data=data, headers=headers, method=method)
  for attempt in range(_MAX_ATTEMPTS):
    try:
      with urllib.request.urlopen(prepared) as response:
        payload = response.read()
      return json.loads(payload) if len(payload) > 0 else None
    except (http.client.HTTPException, OSError) as error:
      if isinstance(error, urllib.error.URLError):
        if not is_transient(error):
          raise
        delay = _retry_delay(error, attempt)
        reason = f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError) else error.reason
      else:
        delay = min(_MAX_BACKOFF, _BASE_BACKOFF * (2**attempt))
        reason = f'{type(error).__name__}: {error}'
      if attempt == _MAX_ATTEMPTS - 1:
        raise
      log.warning(
        f'{reason} from {url}; retrying in {delay:.1f}s (attempt {attempt + 1}/{_MAX_ATTEMPTS})'
      )
      time.sleep(delay)
  raise AssertionError('unreachable: final attempt returns or raises')


def get(url: str, token: str) -> Any:
  return _request('GET', url, token)


def post(url: str, token: str, body: Any) -> Any:
  return _request('POST', url, token, body)


def patch(url: str, token: str, body: Any) -> Any:
  return _request('PATCH', url, token, body)


def delete(url: str, token: str) -> Any:
  return _request('DELETE', url, token)
