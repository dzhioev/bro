"""Synchronous read and write client for the trails service.

`TrailsClient` owns the persistent authenticated HTTPS transport for paged
headers, steps, messages, launch context, and every recording endpoint.

`fetch_recorded_trail` rehydrates the shared `trails.model` records consumed by
`bro.fork`. It follows spill descriptors and inlines their full bodies because
fork replay needs the complete provider response rather than a presigned URL.
"""

import http.client
import json
import ssl
import threading
import time
import urllib.request
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from base import credentials
from trails.model import ForkedFrom, RecordedTrail, Step, Trail, spill_descriptor

DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_STEPS_PAGE_SIZE = 200

_DEFAULT_RETRY_DELAYS_SECONDS = (0.0,)
RECORD_RETRY_DELAYS_SECONDS = (0.1, 0.5, 2.0)


class HTTPStatusError(Exception):
  """A non-success response carrying its numeric HTTP status."""

  def __init__(self, status: int, message: str):
    super().__init__(message)
    self.status = status


def is_retryable_status(status: int) -> bool:
  return status >= 500 or status == 429


class TrailsClient:
  """synchronous HTTPS client for the trails server.

  one persistent connection per client; transport blips drop the socket and
  reopen on the next attempt. the transport lock also lets a recording adapter
  share the connection safely with its keepalive thread.
  """

  def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
    self._base_url = base_url.rstrip('/')
    self._token = token
    self._timeout = timeout
    parsed = urlparse(self._base_url)
    if parsed.scheme != 'https':
      raise ValueError(f'TrailsClient requires an https URL, got {base_url!r}')
    hostname = parsed.hostname
    self._host: str = hostname if hostname is not None else 'localhost'
    self._port = parsed.port
    self._connection: Optional[http.client.HTTPSConnection] = None
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

  def iter_trails(
    self,
    *,
    harness: Optional[str] = None,
    bro: Optional[str] = None,
    forked_from: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    page_size: int = DEFAULT_LIST_PAGE_SIZE,
    max_items: Optional[int] = None,
  ) -> Iterator[dict]:
    """walk every matching trail header across cursor pages. `max_items` caps
    the total — useful for the CLI's `--limit` flag.
    """
    yielded = 0
    cursor: Optional[str] = None
    while True:
      page = self.list_trails(
        harness=harness,
        bro=bro,
        forked_from=forked_from,
        since=since,
        until=until,
        cursor=cursor,
        limit=page_size,
      )
      for trail in page['trails']:
        yield trail
        yielded += 1
        if max_items is not None and yielded >= max_items:
          return
      cursor = page.get('next')
      if cursor is None:
        return

  def get_trail(self, trail_id: str) -> dict:
    return self._get(f'/v1/trails/{trail_id}', {})

  def find_steps_by_uuid(self, uuids: set[str]) -> list[dict]:
    if len(uuids) == 0:
      return []
    query = [('uuid', uuid) for uuid in sorted(uuids)]
    return self._get_pairs('/v1/steps', query)['steps']

  def get_step(self, trail_id: str, step_id: str | int) -> dict:
    return self._get(f'/v1/trails/{trail_id}/steps/{step_id}', {})

  def get_step_uuids(self, trail_id: str, *, through: Optional[str | int] = None) -> list[dict]:
    query = {} if through is None else {'through': str(through)}
    return self._get(f'/v1/trails/{trail_id}/steps/uuids', query)['steps']

  def get_steps(
    self,
    trail_id: str,
    *,
    after: Optional[str | int] = None,
    limit: Optional[int] = None,
  ) -> dict:
    query: dict[str, str] = {}
    if after is not None:
      query['after'] = str(after)
    if limit is not None:
      query['limit'] = str(limit)
    return self._get(f'/v1/trails/{trail_id}/steps', query)

  def iter_steps(
    self,
    trail_id: str,
    *,
    after: Optional[str | int] = None,
    page_size: int = DEFAULT_STEPS_PAGE_SIZE,
  ) -> Iterator[dict]:
    """walk steps across cursor pages. `after` starts the walk strictly past
    that step id (the server's exclusive-start cursor), so an incremental
    caller can resume from the last step it has seen.
    """
    while True:
      page = self.get_steps(trail_id, after=after, limit=page_size)
      yield from page['steps']
      after = page.get('next')
      if after is None:
        return

  def get_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[str | int] = None,
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

  def iter_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[str | int] = None,
    page_size: int = DEFAULT_STEPS_PAGE_SIZE,
  ) -> Iterator[dict]:
    while True:
      page = self.get_messages(trail_id, types=types, after=after, limit=page_size)
      yield from page['messages']
      after = page.get('next')
      if after is None:
        return

  def get_launch_context(self, trail_id: str) -> Optional[Any]:
    """the trail's stored launch-context document, or None when it has none."""
    try:
      return self._get(f'/v1/trails/{trail_id}/context', {})['launch_context']
    except HTTPStatusError as exception:
      if exception.status == 404:
        return None
      raise

  def create_trail(self, payload: dict) -> dict:
    """open a trail (`POST /v1/trails`, harness-native `body` envelope included);
    returns `{id, started_at}`. deliberately not retried: creation is the one
    non-idempotent write, and a duplicate from a lost response would strand an
    orphan trail — the caller's own next attempt is the retry.
    """
    return self._send('POST', '/v1/trails', payload, retry_delays=())

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
      retry_delays=RECORD_RETRY_DELAYS_SECONDS,
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

  def update_header(self, trail_id: str, changes: dict) -> dict:
    """apply a constrained mutable-field upsert (`PATCH /v1/trails/{id}`);
    returns the updated header."""
    return self._send('PATCH', f'/v1/trails/{trail_id}', changes)

  def end_trail(
    self,
    trail_id: str,
    reason: str,
    detail: Optional[str] = None,
    *,
    step_id: Optional[str] = None,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS_SECONDS,
  ) -> None:
    payload: dict[str, Any] = {'reason': reason}
    if detail is not None:
      payload['detail'] = detail
    if step_id is not None:
      payload['step_id'] = step_id
    self._send('POST', f'/v1/trails/{trail_id}/end', payload, retry_delays=retry_delays)

  def keepalive(
    self,
    trail_id: str,
    *,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS_SECONDS,
  ) -> None:
    self._send('POST', f'/v1/trails/{trail_id}/keepalive', {}, retry_delays=retry_delays)

  def close(self) -> None:
    with self._lock:
      self._drop_connection()

  def __enter__(self) -> 'TrailsClient':
    return self

  def __exit__(
    self,
    exception_type: Optional[type[BaseException]],
    exception: Optional[BaseException],
    traceback: Optional[TracebackType],
  ) -> None:
    self.close()

  def fetch_spilled_body(self, url: str) -> Any:
    """download a spilled step body from its presigned S3 URL and parse it the
    same way the server resolves an inline body (`storage._resolve_body`): JSON
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
            raise HTTPStatusError(
              response.status,
              f'{method} {path} -> HTTP {response.status}: {raw.decode(errors="replace")}',
            )
          if response.status == 204 or len(raw) == 0:
            return {}
          return json.loads(raw)
        except HTTPStatusError as exception:
          self._drop_connection()
          if not is_retryable_status(exception.status):
            raise
          last_exception = exception
        except Exception as exception:
          last_exception = exception
          self._drop_connection()
    assert last_exception is not None
    raise last_exception

  def _get_connection(self) -> http.client.HTTPSConnection:
    if self._connection is not None:
      return self._connection
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


def default_client() -> TrailsClient:
  """build a `TrailsClient` from the shared `trails` secret."""
  config = credentials.get_json('trails')
  return TrailsClient(config['base_url'], config['token'])


# fields the server stamps onto every step row alongside the per-kind extras.
# everything else goes into `Step.extras` so callers can inspect provider- and
# kind-specific metadata (turn_index, call_index, tool_name, call_id, response_id, ...).
_STEP_CANONICAL_FIELDS = frozenset(
  {'trail_id', 'step_id', 'ts', 'kind', 'body', 'usage', 'payload_sha256'}
)


def trail_from_header(data: dict) -> Trail:
  """rehydrate a server header dict into the typed `Trail` dataclass.

  raises `KeyError` if a required field is missing — the server always emits
  the full header (nullable fields land as `None`), so a `KeyError` means the
  payload came from somewhere unexpected.
  """
  forked_from_data = data.get('forked_from')
  forked_from = ForkedFrom(**forked_from_data) if forked_from_data is not None else None
  return Trail(
    id=data['id'],
    harness=data['harness'],
    bro=data.get('bro'),
    version=data['version'],
    native=data['native'],
    started_at=data['started_at'],
    interactive=data['interactive'],
    surface=data['surface'],
    forked_from=forked_from,
    summoned_by=data.get('summoned_by'),
  )


def step_from_row(data: dict) -> Step:
  """rehydrate a server step row into the typed `Step` dataclass.

  splits the canonical step fields off into the dataclass attributes and packs
  every other key (turn_index, call_index, tool_name, arguments, call_id,
  response_id, ...) into `extras`.
  """
  extras = {k: v for k, v in data.items() if k not in _STEP_CANONICAL_FIELDS}
  return Step(
    trail_id=data['trail_id'],
    step_id=data['step_id'],
    ts=data['ts'],
    kind=data['kind'],
    body=data.get('body'),
    extras=extras,
    usage=data.get('usage'),
  )


def fetch_recorded_trail(client: TrailsClient, trail_id: str) -> RecordedTrail:
  """fetch a trail header and all of its steps and return them as a
  `RecordedTrail` — the shape `bro.fork.fork()` and `replay_messages` consume.
  """
  header = trail_from_header(client.get_trail(trail_id))
  steps = []
  for row in client.iter_steps(trail_id):
    # follow any spill descriptor so the rehydrated step carries the full body;
    # large `llm_call.response.output`s would otherwise be lost from replay.
    steps.append(step_from_row({**row, 'body': client.resolve_body(row.get('body'))}))
  return RecordedTrail(header=header, steps=steps)
