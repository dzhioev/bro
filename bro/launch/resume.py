"""Resume machinery for reopening a recorded call conversation."""

from dataclasses import dataclass
from typing import Optional

from bro.fork import fork, latest_fork_point
from bro.llm.llm import NativeLLMSpec
from bro.trails.client import TrailsClient, fetch_recorded_trail
from bro.trails.display import DisplayRecord, RecordedAdapter
from bros.bro import Bro

RESUME_LATEST = 'latest'
_LATEST_SCAN_LIMIT = 200
_CALL_ENTRY_POINT = 'call'


@dataclass(frozen=True)
class ResumedCall:
  bro: Bro
  history: list[DisplayRecord]
  trail_id: str


def find_latest_call_trail(client: TrailsClient, bro_name: str) -> Optional[str]:
  """Return the bro's newest recorded call conversation, when one exists."""
  for header in client.iter_trails(bro=bro_name, max_items=_LATEST_SCAN_LIMIT):
    if header.get('surface') == _CALL_ENTRY_POINT:
      return header['id']
  return None


def conversation_history(client: TrailsClient, trail_id: str) -> list[DisplayRecord]:
  """Normalize the recorded conversation and its exact fork-bounded ancestors."""
  target = client.get_trail(trail_id)
  return RecordedAdapter(client).conversation_records(target)


def resume(
  client: TrailsClient,
  bro_name: str,
  trail_ref: str,
  *,
  llm_spec: NativeLLMSpec,
  at: Optional[int] = None,
) -> ResumedCall:
  """Continue a recorded call at an explicit or latest consistent fork point."""
  if trail_ref == RESUME_LATEST:
    found = find_latest_call_trail(client, bro_name)
    if found is None:
      raise ValueError(
        f'no call conversation found for {bro_name} among its {_LATEST_SCAN_LIMIT} newest trails'
      )
    trail_id = found
  else:
    trail_id = trail_ref
  trail = fetch_recorded_trail(client, trail_id)
  if trail.header.bro != bro_name:
    raise ValueError(f'trail {trail_id} belongs to bro {trail.header.bro!r}, not {bro_name!r}')
  history = conversation_history(client, trail_id)
  fork_step_id = at if at is not None else latest_fork_point(trail)
  bro = fork(
    trail,
    fork_step_id,
    llm_spec=llm_spec,
    surface=_CALL_ENTRY_POINT,
    fetch_forked_from=lambda forked_from_id: fetch_recorded_trail(client, forked_from_id),
  )
  return ResumedCall(bro=bro, history=history, trail_id=trail_id)
