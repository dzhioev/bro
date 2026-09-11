"""Store-neutral trails facade and credential-level backend selection."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

from bro.base import credentials
from bro.trails import formats
from bro.trails.model import (
  BlazeRequest,
  ForkedFrom,
  RecordedTrail,
  Step,
  Trail,
  canonical_json_bytes,
  named_tool_digests,
)
from bro.workspace import paths

DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_STEPS_PAGE_SIZE = 200
IMPORT_CHUNK_ROWS = 100
IMPORT_CHUNK_BYTES = 8 * 1024 * 1024


class TrailNotFound(Exception):
  def __init__(self, trail_id: str):
    super().__init__(f'trail not found: {trail_id}')
    self.trail_id = trail_id


class ToolNotFound(Exception):
  def __init__(self, sha256: str):
    super().__init__(f'tool blob not found: {sha256}')
    self.sha256 = sha256


class TrailCollision(Exception):
  """The id an import names already holds a different trail."""

  def __init__(self, trail_id: str, message: str):
    super().__init__(message)
    self.trail_id = trail_id


def collision(trail_id: str, difference: str) -> TrailCollision:
  return TrailCollision(trail_id, f'trail {trail_id} already holds a different trail: {difference}')


class AppendConflict(Exception):
  def __init__(self, expected: int, actual: int):
    super().__init__(f'append offset {expected} does not match trail extent {actual}')
    self.expected = expected
    self.actual = actual


class TransientUnavailable(Exception):
  pass


class InvalidRequest(ValueError):
  """The store refused what a writer sent: no backend will ever accept it, and
  no retry changes that."""


class UnsupportedOperation(Exception):
  """The hosted backend does not serve this operation."""


class PermissionDenied(Exception):
  """The trails token does not carry the permission this operation needs."""


class TrailHasForks(Exception):
  def __init__(self, trail_id: str, forks: list[str]):
    super().__init__(f'trail {trail_id} has forks: {", ".join(forks)}')
    self.trail_id = trail_id
    self.forks = forks


@contextmanager
def refusing_invalid_requests(description: str) -> Iterator[None]:
  try:
    yield
  except InvalidRequest:
    raise
  except (KeyError, TypeError, ValueError) as exception:
    raise InvalidRequest(f'{description}: {exception}') from exception


class TrailsStore(ABC):
  @abstractmethod
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
  ) -> dict: ...

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

  @abstractmethod
  def get_trail(self, trail_id: str) -> dict: ...

  @abstractmethod
  def get_step(self, trail_id: str, step_id: int) -> dict: ...

  @abstractmethod
  def get_steps(
    self, trail_id: str, *, after: Optional[int] = None, limit: Optional[int] = None
  ) -> dict: ...

  def iter_steps(
    self,
    trail_id: str,
    *,
    after: Optional[int] = None,
    page_size: int = DEFAULT_STEPS_PAGE_SIZE,
  ) -> Iterator[dict]:
    while True:
      page = self.get_steps(trail_id, after=after, limit=page_size)
      yield from page['steps']
      after = page.get('next')
      if after is None:
        return

  @abstractmethod
  def get_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[int] = None,
    limit: Optional[int] = None,
  ) -> dict: ...

  def iter_messages(
    self,
    trail_id: str,
    *,
    types: Optional[set[str]] = None,
    after: Optional[int] = None,
    page_size: int = DEFAULT_STEPS_PAGE_SIZE,
  ) -> Iterator[dict]:
    while True:
      page = self.get_messages(trail_id, types=types, after=after, limit=page_size)
      yield from page['messages']
      after = page.get('next')
      if after is None:
        return

  @abstractmethod
  def get_launch_context(self, trail_id: str) -> Optional[Any]: ...

  @abstractmethod
  def blaze(self, request: BlazeRequest) -> dict: ...

  @abstractmethod
  def append_records(
    self,
    trail_id: str,
    offset: int,
    records: list[Any],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict: ...

  @abstractmethod
  def set_subject(self, trail_id: str, subject: Optional[str]) -> dict: ...

  @abstractmethod
  def end_trail(
    self,
    trail_id: str,
    reason: str,
    detail: Optional[str] = None,
  ) -> None: ...

  @abstractmethod
  def keepalive(self, trail_id: str) -> None: ...

  @abstractmethod
  def migrate_trail(self, trail_id: str) -> dict: ...

  @abstractmethod
  def delete_trail(self, trail_id: str) -> dict:
    """Remove a trail and everything only it holds, after recording a manifest of
    what went; returns `{trail_id, extent, manifest}`."""

  @abstractmethod
  def get_tool(self, sha256: str) -> Any:
    """The tool blob stored under its content digest; `ToolNotFound` when the
    store holds none."""

  @abstractmethod
  def begin_import(self, header: dict, *, launch_context: Optional[Any] = None) -> dict:
    """Create the trail a recorded `header` describes, unsealed and marked with
    the extent and `end` it was recorded with, its rows to follow through
    `import_rows`; returns `{trail_id, extent, created}`. The parents the header
    points at must already be stored. An existing trail answers as itself when
    it is the same import under way, or a sealed trail with the same header,
    launch context, extent and end; any other is a `TrailCollision`."""

  @abstractmethod
  def import_rows(
    self,
    trail_id: str,
    offset: int,
    rows: list[dict],
    *,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    """Store recorded rows verbatim from ordinal `offset`, along with the tool
    blobs in `tools`; every blob the rows name must be carried or already
    stored. Rows already present are verified by digest and skipped, a chunk
    starting past the extent is an `AppendConflict`, and a sealed trail takes
    no new row; returns `{extent, appended}`."""

  @abstractmethod
  def seal_import(self, trail_id: str) -> dict:
    """Finish the import `begin_import` marked: refuse unless every recorded
    row is stored, refold the header's aggregate from them, record the `end`
    and drop the mark; returns `{trail_id, extent}`, a no-op on a sealed
    trail."""

  def import_trail(
    self,
    header: dict,
    rows: list[dict],
    *,
    launch_context: Optional[Any] = None,
    tools: Optional[dict[str, Any]] = None,
  ) -> dict:
    """Import one recorded trail whole: begin, every row in chunks, seal. The
    rows must be every one the header records, so a sealed trail answering as
    itself was compared against all of them; blobs in `tools` travel with the
    first chunk naming them."""
    if header.get('extent') != len(rows):
      raise ValueError(f'header records {header.get("extent")!r} rows, {len(rows)} given')
    trail_id = self.begin_import(header, launch_context=launch_context)['trail_id']
    available = {} if tools is None else tools
    for offset, chunk in import_chunks(rows):
      carried = {
        digest: available[digest]
        for digest in sorted(named_tool_digests(chunk))
        if digest in available
      }
      self.import_rows(trail_id, offset, chunk, tools=carried)
      available = {digest: blob for digest, blob in available.items() if digest not in carried}
    return self.seal_import(trail_id)

  def resolve_body(self, body: Any) -> Any:
    return body

  @abstractmethod
  def close(self) -> None: ...

  def __enter__(self) -> 'TrailsStore':
    return self

  def __exit__(
    self,
    exception_type: Optional[type[BaseException]],
    exception: Optional[BaseException],
    traceback: Optional[TracebackType],
  ) -> None:
    self.close()


def import_chunks(rows: list[dict]) -> Iterator[tuple[int, list[dict]]]:
  """`rows` cut into the chunks an import sends, each with its starting ordinal;
  a chunk closes at either bound, and a row over the byte budget travels alone."""
  chunk: list[dict] = []
  chunk_bytes = 0
  offset = 0
  for row in rows:
    size = len(canonical_json_bytes(row))
    if len(chunk) > 0 and (
      len(chunk) >= IMPORT_CHUNK_ROWS or chunk_bytes + size > IMPORT_CHUNK_BYTES
    ):
      yield offset, chunk
      offset += len(chunk)
      chunk = []
      chunk_bytes = 0
    chunk.append(row)
    chunk_bytes += size
  if len(chunk) > 0:
    yield offset, chunk


def local_root() -> Path:
  """the local backend's global runtime root."""
  return paths.trails_dir()


_TRAILS_SECRET = 'trails'
_LOCAL_BACKEND = 'local'
_SERVICE_BACKEND = 'service'


def _backend(config: dict[str, Any]) -> str:
  return config.get('backend', _SERVICE_BACKEND)


def resolve_config(store: credentials.Store) -> dict[str, Any]:
  """the trails configuration a process resolves through `store`: its `trails`
  credential, or local storage where that credential does not resolve —
  configuring the credential is what opts a deployment into the service or dynamo
  backends."""
  if not store.available(_TRAILS_SECRET):
    return {'backend': _LOCAL_BACKEND}
  return store.get_json(_TRAILS_SECRET)


def selects_local_storage(store: credentials.Store) -> bool:
  """whether `resolve_config(store)` records to the local filesystem."""
  return _backend(resolve_config(store)) == _LOCAL_BACKEND


def build_store(config: dict[str, Any]) -> TrailsStore:
  backend = _backend(config)
  if backend == _SERVICE_BACKEND:
    from bro.trails.network import NetworkStore

    try:
      base_url = config['base_url']
      token = config['token']
    except KeyError as exception:
      raise ValueError(f'trails service config is missing {exception.args[0]!r}') from exception
    if not isinstance(base_url, str) or not isinstance(token, str):
      raise ValueError('trails service base_url and token must be strings')
    return NetworkStore(base_url, token)
  if backend == _LOCAL_BACKEND:
    from bro.trails.local import LocalStore

    return LocalStore(local_root())
  if backend == 'dynamo':
    from bro.trails.server.dynamo import build_dynamo_store

    return build_dynamo_store(config)
  raise ValueError(f'unknown trails backend {backend!r}; known: dynamo, local, service')


def default_store() -> TrailsStore:
  return build_store(resolve_config(credentials.default_store()))


def configured_store() -> TrailsStore:
  """the store the `trails` credential names, required: unlike `default_store`,
  an unresolvable credential raises instead of selecting local storage."""
  return build_store(credentials.get_json(_TRAILS_SECRET))


_STEP_CANONICAL_FIELDS = frozenset(
  {'trail_id', 'step_id', 'ts', 'kind', 'body', 'usage', 'payload_sha256', 'format'}
)


def trail_from_header(data: dict) -> Trail:
  data = formats.upgrade_header(data)
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
    hold=data.get('hold'),
    format=data['format'],
  )


def step_from_row(data: dict) -> Step:
  data = formats.upgrade_row(data)
  extras = {key: value for key, value in data.items() if key not in _STEP_CANONICAL_FIELDS}
  return Step(
    trail_id=data['trail_id'],
    step_id=data['step_id'],
    ts=data['ts'],
    kind=data['kind'],
    body=data.get('body'),
    extras=extras,
    usage=data.get('usage'),
    format=data['format'],
  )


def refuse_while_forked(store: TrailsStore, trail_id: str) -> None:
  """Raise while any trail's lineage still points at this one: a fork's chain
  walk resolves every ancestor, so removing one out from under it would leave a
  `forked_from` pointing at nothing."""
  forks = [trail['id'] for trail in store.iter_trails(forked_from=trail_id)]
  if len(forks) > 0:
    raise TrailHasForks(trail_id, forks)


def manifest_name(trail_id: str, at: str) -> str:
  """The file name a manifest for one operation on `trail_id` takes."""
  compact = at.replace(':', '').replace('.', '')
  return f'{trail_id}-{compact}.json'


def delete_manifest(*, trail_id: str, at: str, header: dict, steps: list[dict]) -> dict:
  """The record a backend writes before removing a trail — everything the delete
  takes away, so what was there is still readable afterwards."""
  return {
    'operation': 'delete',
    'at': at,
    'trail_id': trail_id,
    'header': header,
    'steps': steps,
  }


def fetch_recorded_trail(store: TrailsStore, trail_id: str) -> RecordedTrail:
  header = trail_from_header(store.get_trail(trail_id))
  steps = [
    step_from_row({**row, 'body': store.resolve_body(row.get('body'))})
    for row in store.iter_steps(trail_id)
  ]
  return RecordedTrail(header=header, steps=steps)
