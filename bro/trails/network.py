"""Synchronous network proxy for a trails server."""

import http.client
import json
import ssl
import threading
import time
import urllib.request
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from bro.trails.model import (
  LOOPBACK_HOSTS,
  BlazeRequest,
  reported_missing_trail,
  spill_descriptor,
)
from bro.trails.store import (
  AppendConflict,
  TrailNotFound,
  TrailsStore,
  TransientUnavailable,
  UnsupportedOperation,
)

_DEFAULT_RETRY_DELAYS_SECONDS = (0.0,)
_HARD_RETRY_DELAYS_SECONDS = (0.1, 0.5, 2.0)
_KEEPALIVE_RETRY_DELAYS_SECONDS = (0.5,)


class HTTPStatusError(Exception):
  """A non-success response carrying its numeric HTTP status."""

  def __init__(self, status: int, message: str):
    super().__init__(message)
    self.status = status


def is_retryable_status(status: int) -> bool:
  return status >= 500 or status == 429


def _append_conflict_extents(raw: bytes) -> Optional[tuple[int, int]]:
  """the offset and extent an append conflict reports, or None for a 409 from
  another conditional write — the administration repairs answer with their own
  reason."""
  try:
    conflict = json.loads(raw)
  except json.JSONDecodeError:
    return None
  if not isinstance(conflict, dict) or 'expected' not in conflict or 'extent' not in conflict:
    return None
  return conflict['expected'], conflict['extent']


class NetworkStore(TrailsStore):
  """synchronous transport proxy for a trails server.

  one persistent connection per store; transport blips drop the socket and
  reopen on the next attempt. the transport lock also lets a recording adapter
  share the connection safely with its keepalive thread.
  """

  def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
    self._base_url = base_url.rstrip('/')
    self._token = token
    self._timeout = timeout
    parsed = urlparse(self._base_url)
    hostname = parsed.hostname
    secure = parsed.scheme == 'https'
    loopback_http = parsed.scheme == 'http' and hostname in LOOPBACK_HOSTS
    if hostname is None or not (secure or loopback_http):
      raise ValueError(f'NetworkStore requires https or loopback http, got {base_url!r}')
    self._scheme = parsed.scheme
    self._host = hostname
    self._port = parsed.port
    self._connection: Optional[http.client.HTTPConnection] = None
    self._lock = threading.RLock()

  def list_trails(
    self,
    *,
    harness: Optional[str] = None,
    bro: Optional[str] = None,
    forked_from: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
  ) -> dict:
    """one page of trail headers. server caps `limit` at 100; the response
    `next` is the opaque cursor for the next page (or None when exhausted).
    """
    query: dict[str, str] = {}
    if harness is not None:
      query['harness'] = harness
    if bro is not None:
      query['bro'] = bro
    if forked_from is not None:
      query['forked_from'] = forked_from
    if since is not None:
      query['since'] = since
    if until is not None:
      query['until'] = until
    if cursor is not None:
      query['cursor'] = cursor
    if limit is not None:
      query['limit'] = str(limit)
    return self._get('/v1/trails', query)

  def get_trail(self, trail_id: str) -> dict:
    return self._get(f'/v1/trails/{trail_id}', {})

  def get_step(self, trail_id: str, step_id: int) -> dict:
    return self._get(f'/v1/trails/{trail_id}/steps/{step_id}', {})

  def get_steps(
    self,
    trail_id: str,
    *,
    after: Optional[int] = None,
    limit: Optional[int] = None,
  ) -> dict:
    query: dict[str, str] = {}
    if after is not None:
      query['after'] = str(after)
    if limit is not None:
      query['limit'] = str(limit)
    return self._get(f'/v1/trails/{trail_id}/steps', query)

  def get_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[int] = None,
    limit: Optional[int] = None,
  ) -> dict:
    query: list[tuple[str, str]] = []
    if types is not None:
      query.extend(('type', message_type) for message_type in sorted(types))
    if after is not None:
      query.append(('after', str(after)))
    if limit is not None:
      query.append(('limit', str(limit)))
    return self._get_pairs(f'/v1/trails/{trail_id}/messages', query)

  def get_launch_context(self, trail_id: str) -> Optional[Any]:
    """the trail's stored launch-context document, or None when it has none."""
    return self._get(f'/v1/trails/{trail_id}/context', {})['launch_context']

  def blaze(self, request: BlazeRequest) -> dict:
    """open a trail (`POST /v1/trails`, harness-native `body` envelope included);
    returns `{id, started_at}`. not retried here: the caller's own next attempt
    is the retry, and the request's attempt key is what makes that attempt land
    on the trail a lost response already opened instead of a second one.
    """
    return self._send('POST', '/v1/trails', request.to_wire(), retry_delays=())

  def append_records(
    self,
    trail_id: str,
    offset: int,
    records: list[Any],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    payload: dict[str, Any] = {'offset': offset, 'records': records}
    if tools is not None:
      payload['tools'] = tools
    return self._send(
      'POST',
      f'/v1/trails/{trail_id}/records',
      payload,
      retry_delays=_HARD_RETRY_DELAYS_SECONDS,
    )

  def recompute(self, trail_id: str) -> dict:
    return self._send('POST', f'/v1/admin/trails/{trail_id}/recompute', {})

  def check(self, trail_id: Optional[str] = None) -> dict:
    return self._send(
      'POST',
      '/v1/admin/trails/check',
      {'trail_id': trail_id} if trail_id is not None else {},
    )

  def relink(self, trail_id: str, forked_from: dict, delete_count: int) -> dict:
    return self._send(
      'POST',
      f'/v1/admin/trails/{trail_id}/relink',
      {'forked_from': forked_from, 'delete_count': delete_count},
    )

  def set_subject(self, trail_id: str, subject: Optional[str]) -> dict:
    """name the trail (`PATCH /v1/trails/{id}`); returns the updated header."""
    return self._send('PATCH', f'/v1/trails/{trail_id}', {'subject': subject})

  def end_trail(
    self,
    trail_id: str,
    reason: str,
    detail: Optional[str] = None,
  ) -> None:
    payload: dict[str, Any] = {'reason': reason}
    if detail is not None:
      payload['detail'] = detail
    self._send(
      'POST',
      f'/v1/trails/{trail_id}/end',
      payload,
      retry_delays=_HARD_RETRY_DELAYS_SECONDS,
    )

  def keepalive(self, trail_id: str) -> None:
    self._send(
      'POST',
      f'/v1/trails/{trail_id}/keepalive',
      {},
      retry_delays=_KEEPALIVE_RETRY_DELAYS_SECONDS,
    )

  def close(self) -> None:
    with self._lock:
      self._drop_connection()

  def fetch_spilled_body(self, url: str) -> Any:
    """download a spilled step body from its presigned S3 URL and parse it the
    same way the server resolves an inline body (`DynamoStore._resolve_body`): JSON
    when it decodes, raw text otherwise. the URL is self-authenticating, so this
    bypasses the bearer-token connection and hits S3 directly.
    """
    with urllib.request.urlopen(url, timeout=self._timeout) as response:
      raw = response.read()
    try:
      return json.loads(raw)
    except json.JSONDecodeError:
      return raw.decode('utf-8')

  def resolve_body(self, body: Any) -> Any:
    """return a step body's full content: the body itself when inline, the
    fetched content when it is a spill descriptor (`{'s3', 'url', 'size'}`).
    """
    descriptor = spill_descriptor(body)
    if descriptor is None:
      return body
    return self.fetch_spilled_body(descriptor['url'])

  def _get(self, path: str, query: dict[str, str]) -> dict:
    return self._get_pairs(path, list(query.items()))

  def _get_pairs(self, path: str, query: list[tuple[str, str]]) -> dict:
    if len(query) > 0:
      path = f'{path}?{urlencode(query)}'
    headers = {'Authorization': f'Bearer {self._token}'}
    return self._request('GET', path, headers, body=None)

  def _send(
    self,
    method: str,
    path: str,
    payload: dict,
    *,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS_SECONDS,
  ) -> dict:
    headers = {
      'Authorization': f'Bearer {self._token}',
      'Content-Type': 'application/json',
    }
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return self._request(method, path, headers, body, retry_delays=retry_delays)

  def _request(
    self,
    method: str,
    path: str,
    headers: dict,
    body: Optional[bytes],
    *,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS_SECONDS,
  ) -> dict:
    last_exception: Optional[Exception] = None
    schedule = (0.0,) + retry_delays
    with self._lock:
      for delay in schedule:
        if delay > 0:
          time.sleep(delay)
        connection = self._get_connection()
        try:
          connection.request(method, path, body=body, headers=headers)
          response = connection.getresponse()
          raw = response.read()
          if response.status >= 400:
            exception = HTTPStatusError(
              response.status,
              f'{method} {path} -> HTTP {response.status}: {raw.decode(errors="replace")}',
            )
            if response.status == 404:
              missing_trail = reported_missing_trail(raw)
              if missing_trail is not None:
                raise TrailNotFound(missing_trail) from exception
            extents = _append_conflict_extents(raw)
            if response.status == 409 and extents is not None:
              raise AppendConflict(*extents) from exception
            if response.status == 501:
              raise UnsupportedOperation(str(exception)) from exception
            if is_retryable_status(response.status):
              raise TransientUnavailable(str(exception)) from exception
            raise exception
          if response.status == 204 or len(raw) == 0:
            return {}
          return json.loads(raw)
        except (
          TrailNotFound,
          AppendConflict,
          UnsupportedOperation,
          HTTPStatusError,
          ValueError,
          KeyError,
          TypeError,
        ):
          self._drop_connection()
          raise
        except TransientUnavailable as exception:
          last_exception = exception
          self._drop_connection()
        except (OSError, http.client.HTTPException) as exception:
          last_exception = exception
          self._drop_connection()
    assert last_exception is not None
    if isinstance(last_exception, TransientUnavailable):
      raise last_exception
    raise TransientUnavailable(str(last_exception)) from last_exception

  def _get_connection(self) -> http.client.HTTPConnection:
    if self._connection is not None:
      return self._connection
    if self._scheme == 'http':
      self._connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
    else:
      context = ssl.create_default_context()
      self._connection = http.client.HTTPSConnection(
        self._host, self._port, timeout=self._timeout, context=context
      )
    return self._connection

  def _drop_connection(self) -> None:
    if self._connection is None:
      return
    try:
      self._connection.close()
    except Exception:
      pass
    self._connection = None
