"""Store-neutral trails facade and credential-level backend selection."""

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

from bro.base import credentials
from bro.trails.model import ForkedFrom, RecordedTrail, Step, Trail

DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_STEPS_PAGE_SIZE = 200
RECORD_RETRY_DELAYS_SECONDS = (0.1, 0.5, 2.0)


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
  def find_steps_by_uuid(self, uuids: set[str]) -> list[dict]: ...

  @abstractmethod
  def get_step(self, trail_id: str, step_id: int) -> dict: ...

  @abstractmethod
  def get_step_uuids(self, trail_id: str, *, through: Optional[int] = None) -> list[dict]: ...

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
  def create_trail(self, payload: dict) -> dict: ...

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
  def update_header(self, trail_id: str, changes: dict) -> dict: ...

  @abstractmethod
  def end_trail(
    self,
    trail_id: str,
    reason: str,
    detail: Optional[str] = None,
    *,
    retry_delays: tuple[float, ...] = (0.0,),
  ) -> None: ...

  @abstractmethod
  def keepalive(self, trail_id: str, *, retry_delays: tuple[float, ...] = (0.0,)) -> None: ...

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
  configured = os.environ.get('BRO_TRAILS_DIR')
  if configured is not None:
    if len(configured) == 0:
      raise ValueError('BRO_TRAILS_DIR must not be empty')
    return Path(configured).expanduser()
  data_home = os.environ.get('XDG_DATA_HOME')
  base = Path(data_home).expanduser() if data_home is not None else Path.home() / '.local' / 'share'
  return base / 'bro'


def build_store(config: dict[str, Any]) -> TrailsStore:
  backend = config.get('backend', 'service')
  if backend == 'service':
    from bro.trails.client import TrailsClient

    try:
      base_url = config['base_url']
      token = config['token']
    except KeyError as exception:
      raise ValueError(f'trails service config is missing {exception.args[0]!r}') from exception
    if not isinstance(base_url, str) or not isinstance(token, str):
      raise ValueError('trails service base_url and token must be strings')
    return TrailsClient(base_url, token)
  if backend == 'local':
    from bro.trails.local import LocalStore

    return LocalStore(local_root())
  raise ValueError(f'unknown trails backend {backend!r}; known: local, service')


def default_store() -> TrailsStore:
  return build_store(credentials.get_json('trails'))


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
