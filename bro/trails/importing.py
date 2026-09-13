"""The import semantics every backend shares: what a recorded header becomes,
how re-sent headers and rows are matched, and what a seal writes."""

from collections.abc import Callable
from typing import Any, Optional

from bro.trails import backends, formats, model, rows
from bro.trails.model import BlazeRequest
from bro.trails.store import collision, refusing_invalid_requests

# what a served header carries over the stored one
_HEADER_PROJECTIONS = frozenset({'usage', 'models'})
# the header fields the row fold owns
_FOLDED_HEADER_FIELDS = frozenset({'extent', 'turn_count', 'last_billed_message_id'})
_IMPORT_STATE = 'importing'
_PARENT_POINTERS = (('forked_from', 'forked from'), ('summoned_by', 'summoned by'))


def imported_header(header: dict, adapter: backends.Adapter) -> dict:
  """The unsealed header an import creates from a recorded one: every field as
  recorded but the read projections, the fold-owned fields and `end`, with the
  server-derived native fold cleared down to the minted lineage cuts, and the
  recorded extent and end held in the import mark. The format stays the
  recorded one; semantic validation reads the upgraded representation."""
  upgraded = formats.upgrade_header(header)
  trail_id = upgraded.get('id')
  if not isinstance(trail_id, str) or len(trail_id) == 0:
    raise ValueError('id must be a non-empty string')
  for field in ('started_at', 'last_alive_at'):
    if not isinstance(upgraded.get(field), str):
      raise ValueError(f'{field} must be a string')
  extent = upgraded.get('extent')
  if not isinstance(extent, int) or isinstance(extent, bool) or extent < 0:
    raise ValueError('extent must be a non-negative int')
  end = upgraded.get('end')
  model.validate_recorded_end(end)
  recorded_native = {
    key: value
    for key, value in header['native'].items()
    if key not in backends.SERVER_DERIVED_NATIVE_FIELDS
  }
  upgraded_native = {
    key: value
    for key, value in upgraded['native'].items()
    if key not in backends.SERVER_DERIVED_NATIVE_FIELDS
  }
  BlazeRequest(
    harness=upgraded['harness'],
    version=upgraded['version'],
    interactive=upgraded['interactive'],
    surface=upgraded['surface'],
    body={},
    native=upgraded_native,
    bro=upgraded.get('bro'),
    hold=upgraded.get('hold'),
    forked_from=upgraded.get('forked_from'),
    summoned_by=upgraded.get('summoned_by'),
    subject=upgraded.get('subject'),
    location=upgraded.get('location'),
  )
  adapter.validate_create(upgraded_native)
  recorded_native.update(rows.replayed_native(adapter, upgraded))
  imported = {
    key: value
    for key, value in header.items()
    if key not in _HEADER_PROJECTIONS
    and key not in _FOLDED_HEADER_FIELDS
    and key not in {'end', _IMPORT_STATE}
  }
  imported.update(
    {
      'native': recorded_native,
      'end': None,
      'extent': 0,
      'turn_count': 0,
      _IMPORT_STATE: {'extent': extent, 'end': end},
    }
  )
  return imported


def import_identity(header: dict, adapter: backends.Adapter) -> dict:
  """What decides whether two recorded headers describe the same trail: the
  imported header in this reader's shape, its format label and import mark
  aside."""
  identity = imported_header(formats.upgrade_header(header), adapter)
  del identity['format']
  del identity[_IMPORT_STATE]
  return identity


def import_state(header: dict) -> Optional[dict]:
  """The import mark a stored header carries while its import is under way;
  None once sealed."""
  state = header.get(_IMPORT_STATE)
  if state is None:
    return None
  if not isinstance(state, dict) or set(state) != {'extent', 'end'}:
    raise ValueError(f'trail {header.get("id")} carries a malformed import mark')
  return state


def require_parents(
  header: dict, stored: Callable[[str], bool], *, external_parents_ok: bool = False
) -> None:
  """Raise unless every trail the header points at is stored.

  `external_parents_ok` accepts a fork/summon pointer whose parent is absent
  because it was recorded to another backend — the shape a summoned child
  adopted into a store its summoner did not record to carries; live recording
  never checks parents, so such a pointer is already a store-consistent state."""
  if external_parents_ok:
    return
  semantic_header = formats.upgrade_header(header)
  for field, relation in _PARENT_POINTERS:
    pointer = semantic_header.get(field)
    if pointer is None:
      continue
    parent = pointer['trail_id']
    if not stored(parent):
      raise ValueError(f'trail {header["id"]} is {relation} {parent}, which must be imported first')


def verify_same_import(
  trail_id: str,
  adapter: backends.Adapter,
  existing: dict,
  imported: dict,
  existing_context: Optional[Any],
  launch_context: Optional[Any],
) -> None:
  """Raise unless the trail already stored is the one being imported: the
  same identity and launch context, then the same import under way, or a
  sealed trail with the recorded extent and end."""
  if import_identity(existing, adapter) != import_identity(imported, adapter):
    raise collision(trail_id, 'header differs')
  if existing_context != launch_context:
    raise collision(trail_id, 'launch context differs')
  announced = imported[_IMPORT_STATE]
  pending = import_state(existing)
  if pending is None:
    if existing.get('extent') != announced['extent']:
      raise collision(
        trail_id, f'{existing.get("extent")} rows stored, {announced["extent"]} recorded'
      )
    if existing.get('end') != announced['end']:
      raise collision(trail_id, 'end differs')
  elif pending != announced:
    raise collision(trail_id, 'another import of it is under way')


def verify_room(trail_id: str, pending: Optional[dict], stored_extent: int, appending: int) -> None:
  """Raise unless `appending` rows past `stored_extent` fit the import: none
  on a sealed trail, and no more than the mark announced on one under way."""
  if appending == 0:
    return
  if pending is None:
    raise collision(trail_id, f'rows past its sealed extent {stored_extent}')
  if stored_extent + appending > pending['extent']:
    raise ValueError(
      f'trail {trail_id} was recorded with {pending["extent"]} rows, '
      f'{stored_extent + appending} sent'
    )


def verify_complete(trail_id: str, pending: dict, stored_extent: int) -> None:
  """Raise unless every row the import mark announced is stored."""
  if stored_extent != pending['extent']:
    raise ValueError(
      f'trail {trail_id} holds {stored_extent} of the {pending["extent"]} rows recorded'
    )


def validate_rows(
  trail_id: str, offset: int, records: list[Any], adapter: backends.Adapter
) -> None:
  """Refuse recorded rows an import carries for `trail_id` from `offset` unless
  each names its ordinal, is in a format this reader knows, and parses under
  the adapter."""
  for step_id, row in enumerate(records, start=offset):
    with refusing_invalid_requests(f'imported row {trail_id}/{step_id}'):
      if not isinstance(row, dict):
        raise ValueError('row must be an object')
      if row.get('trail_id') != trail_id:
        raise ValueError(f'row names trail {row.get("trail_id")!r}')
      if row.get('step_id') != step_id:
        raise ValueError(f'row carries step {row.get("step_id")!r}')
      rows.row_digest(row)
      adapter.parse(formats.upgrade_row(row))


def verify_same_rows(trail_id: str, stored: list[dict], incoming: list[dict]) -> None:
  """Raise unless `incoming` re-sends exactly the rows already stored at its
  ordinals, judged by the digest each row carries."""
  if len(stored) != len(incoming):
    raise ValueError(f'trail {trail_id} holds {len(stored)} of the {len(incoming)} rows compared')
  for stored_row, incoming_row in zip(stored, incoming, strict=True):
    if rows.row_digest(stored_row) != rows.row_digest(incoming_row):
      raise collision(trail_id, f'row {stored_row.get("step_id")} differs')


def sealed_fields(
  header: dict, stored: list[dict], adapter: backends.Adapter, end: Optional[dict]
) -> dict:
  """The header fields a seal writes: the aggregate refolded from every stored
  row, and the recorded `end`. A subject the header already carries stays, and
  a billing id the fold has none of is absent rather than null."""
  model.validate_recorded_end(end)
  trail_id = header['id']
  for step_id, row in enumerate(stored):
    if row.get('step_id') != step_id:
      raise ValueError(f'trail {trail_id} rows are not contiguous at step {step_id}')
  state, _ = rows.replay(formats.upgrade_header(header), stored, adapter)
  fields = rows.state_fields(state, len(stored))
  recorded_native = {
    key: value
    for key, value in header['native'].items()
    if key not in backends.SERVER_DERIVED_NATIVE_FIELDS
  }
  recorded_native.update(
    {
      key: value
      for key, value in state.native.items()
      if key in backends.SERVER_DERIVED_NATIVE_FIELDS
    }
  )
  fields['native'] = recorded_native
  if header.get('subject') is not None:
    fields.pop('subject', None)
  fields['end'] = end
  return fields
