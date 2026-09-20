"""Synchronous network proxy for a trails server."""

import http.client
import json
import ssl
import threading
import time
import urllib.request
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from bro.trails import formats
from bro.trails.model import (
  LOOPBACK_HOSTS,
  BlazeRequest,
  reported_collision,
  reported_forks,
  reported_missing_tool,
  reported_missing_trail,
  spill_descriptor,
)
from bro.trails.store import (
  AppendConflict,
  InvalidRequest,
  PermissionDenied,
  ToolNotFound,
  TrailCollision,
  TrailHasForks,
  TrailNotFound,
  TrailsStore,
  TransientUnavailable,
  UnsupportedOperation,
)

_DEFAULT_RETRY_DELAYS_SECONDS = (0.0,)
_HARD_RETRY_DELAYS_SECONDS = (0.1, 0.5, 2.0)
_KEEPALIVE_RETRY_DELAYS_SECONDS = (0.5,)


class HTTPStatusError(Exception):
  """A non-success response carrying its numeric HTTP status and raw body."""

  def __init__(self, status: int, message: str, body: bytes):
    super().__init__(message)
    self.status = status
    self.body = body


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

  connections are pooled: a request takes an idle one or opens another and
  returns it once answered, and a transport blip or a refused response closes
  the one it was on, so callers on different threads never share a socket.
  """

  # the budget covers a blaze whose lineage resolution walks a long transcript,
  # the one request this store makes that is not answered in well under a second
  def __init__(self, base_url: str, token: str, *, timeout: float = 30.0):
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
    self._idle: list[http.client.HTTPConnection] = []
    self._closed = False
    self._lock = threading.Lock()

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
    page = self._get('/v1/trails', query)
    page['trails'] = [formats.upgrade_header(header) for header in page['trails']]
    return page

  def get_trail(self, trail_id: str) -> dict:
    return formats.upgrade_header(self._get(f'/v1/trails/{trail_id}', {}))

  def get_step(self, trail_id: str, step_id: int) -> dict:
    return formats.upgrade_row(self._get(f'/v1/trails/{trail_id}/steps/{step_id}', {}))

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
    page = self._get(f'/v1/trails/{trail_id}/steps', query)
    page['steps'] = [formats.upgrade_row(row) for row in page['steps']]
    return page

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
    returns `{id, started_at, extent}` plus the resolver's verdict where the
    request carried lineage evidence. deliberately not retried: minting is the
    one non-idempotent write, and a duplicate from a lost response would strand
    an orphan trail — the caller's own next attempt is the retry, and for a
    harness that resolves lineage it converges on that orphan instead.
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

  def migrate_trail(self, trail_id: str) -> dict:
    return self._send('POST', f'/v1/admin/trails/{trail_id}/migrate', {})

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

  def delete_trail(self, trail_id: str) -> dict:
    try:
      return self._send('DELETE', f'/v1/admin/trails/{trail_id}', {})
    except HTTPStatusError as exception:
      forks = reported_forks(exception.body)
      if exception.status == 409 and forks is not None:
        raise TrailHasForks(trail_id, forks) from exception
      raise

  def get_tool(self, sha256: str) -> Any:
    return self._get(f'/v1/tools/{sha256}', {})['tool']

  def begin_import(self, header: dict, *, launch_context: Optional[Any] = None) -> dict:
    trail_id = header.get('id')
    if not isinstance(trail_id, str) or len(trail_id) == 0:
      raise ValueError('imported header id must be a non-empty string')
    payload: dict[str, Any] = {'header': header}
    if launch_context is not None:
      payload['launch_context'] = launch_context
    return self._send(
      'POST',
      f'/v1/admin/trails/{trail_id}/import',
      payload,
      retry_delays=_HARD_RETRY_DELAYS_SECONDS,
    )

  def import_rows(
    self,
    trail_id: str,
    offset: int,
    rows: list[dict],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    payload: dict[str, Any] = {'offset': offset, 'rows': rows}
    if tools is not None:
      payload['tools'] = tools
    return self._send(
      'POST',
      f'/v1/admin/trails/{trail_id}/import/rows',
      payload,
      retry_delays=_HARD_RETRY_DELAYS_SECONDS,
    )

  def seal_import(self, trail_id: str) -> dict:
    return self._send(
      'POST',
      f'/v1/admin/trails/{trail_id}/import/seal',
      {},
      retry_delays=_HARD_RETRY_DELAYS_SECONDS,
    )

  def close(self) -> None:
    """close the idle connections and refuse further requests; a connection out
    on a request closes when that request returns it."""
    with self._lock:
      self._closed = True
      idle, self._idle = self._idle, []
    for connection in idle:
      connection.close()

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
    for delay in (0.0,) + retry_delays:
      if delay > 0:
        time.sleep(delay)
      connection = self._take_connection()
      try:
        result = _exchange(connection, method, path, headers, body)
      except TransientUnavailable as exception:
        last_exception = exception
        connection.close()
      except (OSError, http.client.HTTPException) as exception:
        last_exception = exception
        connection.close()
      except BaseException:
        connection.close()
        raise
      else:
        self._return_connection(connection)
        return result
    assert last_exception is not None
    if isinstance(last_exception, TransientUnavailable):
      raise last_exception
    raise TransientUnavailable(str(last_exception)) from last_exception

  def _take_connection(self) -> http.client.HTTPConnection:
    with self._lock:
      if self._closed:
        raise RuntimeError(f'trails store for {self._base_url} is closed')
      if len(self._idle) > 0:
        return self._idle.pop()
    if self._scheme == 'http':
      return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
    return http.client.HTTPSConnection(
      self._host, self._port, timeout=self._timeout, context=ssl.create_default_context()
    )

  def _return_connection(self, connection: http.client.HTTPConnection) -> None:
    with self._lock:
      if not self._closed:
        self._idle.append(connection)
        return
    connection.close()


def _exchange(
  connection: http.client.HTTPConnection,
  method: str,
  path: str,
  headers: dict,
  body: Optional[bytes],
) -> dict:
  """one request over `connection`, its response mapped onto the store errors."""
  connection.request(method, path, body=body, headers=headers)
  response = connection.getresponse()
  raw = response.read()
  if response.status >= 400:
    exception = HTTPStatusError(
      response.status,
      f'{method} {path} -> HTTP {response.status}: {raw.decode(errors="replace")}',
      raw,
    )
    if response.status == 404:
      missing_trail = reported_missing_trail(raw)
      if missing_trail is not None:
        raise TrailNotFound(missing_trail) from exception
      missing_tool = reported_missing_tool(raw)
      if missing_tool is not None:
        raise ToolNotFound(missing_tool) from exception
    if response.status == 409:
      extents = _append_conflict_extents(raw)
      if extents is not None:
        raise AppendConflict(*extents) from exception
      collided = reported_collision(raw)
      if collided is not None:
        raise TrailCollision(collided, str(exception)) from exception
    if response.status in (401, 403):
      raise PermissionDenied(str(exception)) from exception
    if response.status == 501:
      raise UnsupportedOperation(str(exception)) from exception
    if response.status == 400:
      raise InvalidRequest(str(exception)) from exception
    if is_retryable_status(response.status):
      raise TransientUnavailable(str(exception)) from exception
    raise exception
  if response.status == 204 or len(raw) == 0:
    return {}
  return json.loads(raw)
