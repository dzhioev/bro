"""Trails as directories in the local store layout: exported from any store,
imported into any."""

from pathlib import Path
from typing import Any, Optional, Protocol

from bro.trails import formats, importing
from bro.trails.local import LocalStore
from bro.trails.model import named_tool_digests
from bro.trails.store import ToolNotFound, TrailsStore

_PARENT_POINTERS = ('forked_from', 'summoned_by')


class TrailSource(Protocol):
  """Where a copy reads one trail from."""

  def rows(self, trail_id: str) -> list[dict]: ...

  def launch_context(self, trail_id: str) -> Optional[Any]: ...

  def tool(self, sha256: str) -> Optional[Any]:
    """The blob, or None where the source lacks it and the destination may
    hold it already."""
    ...


class StoreSource:
  """A store read through its contract: what it serves is what travels."""

  def __init__(self, store: TrailsStore):
    self._store = store
    self._tools: dict[str, Any] = {}

  def rows(self, trail_id: str) -> list[dict]:
    return [
      {**row, 'body': self._store.resolve_body(row.get('body'))}
      for row in self._store.iter_steps(trail_id)
    ]

  def launch_context(self, trail_id: str) -> Optional[Any]:
    return self._store.get_launch_context(trail_id)

  def tool(self, sha256: str) -> Any:
    if sha256 not in self._tools:
      self._tools[sha256] = self._store.get_tool(sha256)
    return self._tools[sha256]


class LayoutSource:
  """A local store layout read as stored, each record in the format it was
  written in."""

  def __init__(self, root: Path):
    if not (root / 'trails').is_dir():
      raise ValueError(f'no trails store layout at {root}')
    self._store = LocalStore(root)

  def headers(self) -> list[dict]:
    return [self._store.stored_header(trail_id) for trail_id in self._store.stored_trail_ids()]

  def rows(self, trail_id: str) -> list[dict]:
    return self._store.stored_rows(trail_id)

  def launch_context(self, trail_id: str) -> Optional[Any]:
    return self._store.stored_launch_context(trail_id)

  def tool(self, sha256: str) -> Optional[Any]:
    try:
      return self._store.get_tool(sha256)
    except ToolNotFound:
      return None


def parent_ids(header: dict) -> list[str]:
  """The trails the header points at, through its fork and summon pointers."""
  semantic_header = formats.upgrade_header(header)
  parents: list[str] = []
  for field in _PARENT_POINTERS:
    pointer = semantic_header.get(field)
    if pointer is None:
      continue
    if not isinstance(pointer, dict) or not isinstance(pointer.get('trail_id'), str):
      raise ValueError(f'trail {header.get("id")!r} has a malformed {field}')
    parents.append(pointer['trail_id'])
  return parents


def parents_first(headers: list[dict]) -> list[dict]:
  """The headers ordered so every trail follows the parents among them."""
  by_id = {header['id']: header for header in headers}
  ordered: list[dict] = []
  placed: set[str] = set()
  placing: set[str] = set()

  def place(trail_id: str) -> None:
    if trail_id in placed:
      return
    if trail_id in placing:
      raise ValueError(f'trail lineage cycles through {trail_id}')
    placing.add(trail_id)
    for parent in parent_ids(by_id[trail_id]):
      if parent in by_id:
        place(parent)
    placing.remove(trail_id)
    placed.add(trail_id)
    ordered.append(by_id[trail_id])

  for header in headers:
    place(header['id'])
  return ordered


def ancestry(store: TrailsStore, trail_ids: list[str]) -> list[dict]:
  """The named trails and every ancestor reachable through `forked_from` and
  `summoned_by`, parents first."""
  headers: dict[str, dict] = {}
  pending = list(trail_ids)
  while len(pending) > 0:
    trail_id = pending.pop()
    if trail_id in headers:
      continue
    header = store.get_trail(trail_id)
    headers[trail_id] = header
    pending.extend(parent_ids(header))
  return parents_first(list(headers.values()))


def copy_trail(source: TrailSource, header: dict, destination: TrailsStore) -> dict:
  """Import one trail into `destination` from what `source` holds of it,
  carrying the tool blobs its rows name where the source has them."""
  trail_id = header['id']
  if importing.import_state(header) is not None:
    raise ValueError(f'trail {trail_id} has an import under way')
  rows = source.rows(trail_id)
  tools = {}
  for digest in sorted(named_tool_digests(rows)):
    blob = source.tool(digest)
    if blob is not None:
      tools[digest] = blob
  return destination.import_trail(
    header, rows, launch_context=source.launch_context(trail_id), tools=tools
  )


def export_trails(store: TrailsStore, trail_ids: list[str], root: Path) -> list[dict]:
  """Write the named trails and their ancestry into the local store layout at
  `root`; returns each import's result, parents first."""
  source = StoreSource(store)
  destination = LocalStore(root)
  return [copy_trail(source, header, destination) for header in ancestry(store, trail_ids)]


def import_layout(root: Path, store: TrailsStore) -> list[dict]:
  """Import every trail the layout at `root` holds into `store`, parents
  first; returns each import's result."""
  source = LayoutSource(root)
  return [copy_trail(source, header, store) for header in parents_first(source.headers())]
