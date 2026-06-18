"""read-side client for the trails service.

counterpart to `llm.tracker.HTTPTracker` (the write-side client). `TrailsClient`
wraps the read endpoints — `GET /v1/trails`, `GET /v1/trails/{id}`,
`GET /v1/trails/{id}/steps` — over a persistent HTTPS connection, exposing both
single-page methods (`list_trails` / `get_steps`) and transparent cursor
iterators (`iter_trails` / `iter_steps`) that paginate until exhausted.

`fetch_recorded_trail` rehydrates a header + all of its steps into the
`Trail` / `Step` / `RecordedTrail` dataclasses defined in `llm.tracker`, the
same shape `bro.fork.fork()` consumes — so the typical caller flow is

    trail = fetch_recorded_trail(default_client(), trail_id)
    bro = fork(trail, step_id)

S3 spillover is resolved on the server (`storage._resolve_body`) — step bodies
come back inline when small, as `{'s3': key, 'url': <presigned>, 'size': N}`
when large. clients receive whichever shape the server emits; the CLI surfaces
the spilled form with size + URL rather than fetching multi-MB blobs eagerly.
"""

import http.client
import json
import ssl
from collections.abc import Iterator
from urllib.parse import urlencode, urlparse

import configs
from base import credentials
from llm.tracker import (
  Parent,
  RecordedTrail,
  Step,
  Trail,
)

DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_STEPS_PAGE_SIZE = 200


class TrailsClient:
  """synchronous HTTPS client for the trails server's read endpoints.

  one persistent connection per client; transport blips drop the socket and
  reopen on the next call. write endpoints (`POST /v1/trails`,
  `POST /v1/trails/{id}/steps`, `POST /v1/trails/{id}/end`) are intentionally
  not exposed — bros write through `llm.tracker.HTTPTracker`.
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
    self._conn: http.client.HTTPSConnection | None = None

  def list_trails(
    self,
    *,
    bro: str | None = None,
    parent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
  ) -> dict:
    """one page of trail headers. server caps `limit` at 100; the response
    `next` is the opaque cursor for the next page (or None when exhausted).
    """
    query: dict[str, str] = {}
    if bro is not None:
      query['bro'] = bro
    if parent is not None:
      query['parent'] = parent
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
    bro: str | None = None,
    parent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    page_size: int = DEFAULT_LIST_PAGE_SIZE,
    max_items: int | None = None,
  ) -> Iterator[dict]:
    """walk every matching trail header across cursor pages. `max_items` caps
    the total — useful for the CLI's `--limit` flag.
    """
    yielded = 0
    cursor: str | None = None
    while True:
      page = self.list_trails(
        bro=bro,
        parent=parent,
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

  def get_steps(
    self,
    trail_id: str,
    *,
    after: str | None = None,
    limit: int | None = None,
  ) -> dict:
    query: dict[str, str] = {}
    if after is not None:
      query['after'] = after
    if limit is not None:
      query['limit'] = str(limit)
    return self._get(f'/v1/trails/{trail_id}/steps', query)

  def iter_steps(
    self,
    trail_id: str,
    *,
    page_size: int = DEFAULT_STEPS_PAGE_SIZE,
  ) -> Iterator[dict]:
    after: str | None = None
    while True:
      page = self.get_steps(trail_id, after=after, limit=page_size)
      steps = page['steps']
      for step in steps:
        yield step
      after = page.get('next')
      if after is None:
        return

  def close(self) -> None:
    self._drop_conn()

  def _get(self, path: str, query: dict[str, str]) -> dict:
    if len(query) > 0:
      path = f'{path}?{urlencode(query)}'
    headers = {'Authorization': f'Bearer {self._token}'}
    return self._request('GET', path, headers, body=None)

  def _request(
    self,
    method: str,
    path: str,
    headers: dict,
    body: bytes | None,
  ) -> dict:
    last_exc: Exception | None = None
    # transient blips often leave the persistent socket half-open; one retry on
    # a fresh connection is enough to cover that without dragging in a full
    # backoff schedule (the read path is non-mutating and idempotent).
    for attempt in range(2):
      conn = self._get_conn()
      try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
          raise http.client.HTTPException(
            f'{method} {path} -> HTTP {resp.status}: {raw.decode(errors="replace")}'
          )
        if resp.status == 204 or len(raw) == 0:
          return {}
        return json.loads(raw)
      except Exception as exc:
        last_exc = exc
        self._drop_conn()
        if attempt == 1:
          break
    assert last_exc is not None
    raise last_exc

  def _get_conn(self) -> http.client.HTTPSConnection:
    if self._conn is not None:
      return self._conn
    ctx = ssl.create_default_context()
    self._conn = http.client.HTTPSConnection(
      self._host, self._port, timeout=self._timeout, context=ctx
    )
    return self._conn

  def _drop_conn(self) -> None:
    if self._conn is None:
      return
    try:
      self._conn.close()
    except Exception:
      pass
    self._conn = None


def default_client() -> TrailsClient:
  """build a `TrailsClient` from the `trails` secret — the same credential the
  in-bro `HTTPTracker` reads, so the read and write sides share one source.
  """
  cfg = credentials.get_json('trails')
  return TrailsClient(cfg['base_url'], cfg['token'])


# fields the server stamps onto every step row alongside the per-kind extras.
# everything else goes into `Step.extras` so callers can poke at provider /
# kind-specific metadata (turn_index, tool_name, call_id, response_id, ...).
_STEP_CANONICAL_FIELDS = frozenset({'trail_id', 'step_id', 'ts', 'kind', 'body'})


def trail_from_header(data: dict) -> Trail:
  """rehydrate a server header dict into the typed `Trail` dataclass.

  raises `KeyError` if a required field is missing — the server always emits
  the full header (nullable fields land as `None`), so a `KeyError` means the
  payload came from somewhere unexpected.
  """
  parent_data = data.get('parent')
  parent = Parent(**parent_data) if parent_data is not None else None
  return Trail(
    trail_id=data['trail_id'],
    bro=data['bro'],
    bro_version=data.get('bro_version', configs.VERSION),
    llm_spec=data['llm_spec'],
    started_at=data['started_at'],
    interactive=data['interactive'],
    entry_point=data['entry_point'],
    parent=parent,
  )


def step_from_row(data: dict) -> Step:
  """rehydrate a server step row into the typed `Step` dataclass.

  splits the canonical step fields off into the dataclass attributes and packs
  every other key (turn_index, tool_name, arguments, call_id, response_id,
  tokens_in, ...) into `extras` — mirrors `read_local_file`'s split.
  """
  extras = {k: v for k, v in data.items() if k not in _STEP_CANONICAL_FIELDS}
  return Step(
    trail_id=data['trail_id'],
    step_id=data['step_id'],
    ts=data['ts'],
    kind=data['kind'],
    body=data.get('body'),
    extras=extras,
  )


def fetch_recorded_trail(client: TrailsClient, trail_id: str) -> RecordedTrail:
  """fetch a trail header and all of its steps and return them as a
  `RecordedTrail` — the shape `bro.fork.fork()` and `replay_messages` consume.
  """
  header = trail_from_header(client.get_trail(trail_id))
  steps = [step_from_row(row) for row in client.iter_steps(trail_id)]
  return RecordedTrail(header=header, steps=steps)
