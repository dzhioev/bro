"""Store-neutral trails facade and credential-level backend selection."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

from bro.base import credentials
from bro.trails.model import BlazeRequest, ForkedFrom, RecordedTrail, Step, Trail
from bro.workspace import paths

DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_STEPS_PAGE_SIZE = 200


class TrailNotFound(Exception):
  def __init__(self, trail_id: str):
    super().__init__(f'trail not found: {trail_id}')
    self.trail_id = trail_id


class AppendConflict(Exception):
  def __init__(self, expected: int, actual: int):
    super().__init__(f'append offset {expected} does not match trail extent {actual}')
    self.expected = expected
    self.actual = actual


class TransientUnavailable(Exception):
  pass


class UnsupportedOperation(Exception):
  """The hosted backend does not serve this operation."""


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
  {'trail_id', 'step_id', 'ts', 'kind', 'body', 'usage', 'payload_sha256'}
)


def trail_from_header(data: dict) -> Trail:
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
  )


def step_from_row(data: dict) -> Step:
  extras = {key: value for key, value in data.items() if key not in _STEP_CANONICAL_FIELDS}
  return Step(
    trail_id=data['trail_id'],
    step_id=data['step_id'],
    ts=data['ts'],
    kind=data['kind'],
    body=data.get('body'),
    extras=extras,
    usage=data.get('usage'),
  )


def fetch_recorded_trail(store: TrailsStore, trail_id: str) -> RecordedTrail:
  header = trail_from_header(store.get_trail(trail_id))
  steps = [
    step_from_row({**row, 'body': store.resolve_body(row.get('body'))})
    for row in store.iter_steps(trail_id)
  ]
  return RecordedTrail(header=header, steps=steps)
