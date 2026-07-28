"""resume machinery for `call`: reopen a recorded conversation.

a `call` conversation is recorded as a trail (surface 'call'); `resume`
continues one as a fresh `Bro` preseeded through `bro.fork.fork` at the
trail's latest consistent step. the continuation is itself recorded as a new
'call' trail with a `forked_from` fork pointer, so resumes chain — and
`conversation_history` walks that ancestor chain to rebuild the prior
exchanges (projected user messages and terminal replies) for the UI to render.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bro.bros.bro import Bro
from bro.fork import fork, latest_fork_point
from llm.llm import LLMSpec
from trails.client import TrailsClient, fetch_recorded_trail
from trails.lineage import walk_header_chain

# `--resume` without a trail id: continue the bro's newest recorded `call`
# conversation.
RESUME_LATEST = 'latest'

# how many of the bro's newest trails the latest-conversation lookup scans
# before concluding there is nothing to resume.
_LATEST_SCAN_LIMIT = 200

_CALL_ENTRY_POINT = 'call'


@dataclass(frozen=True)
class HistoryMessage:
  """one prior exchange line of a resumed conversation, ready to render:
  `when` is timezone-aware local time."""

  by_user: bool
  text: str
  when: datetime


@dataclass(frozen=True)
class ResumedCall:
  bro: Bro
  history: list[HistoryMessage]
  # the trail the conversation was resumed from (the new trail's forked_from);
  # the continuation's own id is `bro.trail_id`.
  trail_id: str


def find_latest_call_trail(client: TrailsClient, bro_name: str) -> Optional[str]:
  """the bro's newest recorded `call` conversation, or None when none is found
  among its `_LATEST_SCAN_LIMIT` newest trails."""
  for header in client.iter_trails(bro=bro_name, max_items=_LATEST_SCAN_LIMIT):
    if header.get('surface') == _CALL_ENTRY_POINT:
      return header['id']
  return None


def conversation_history(client: TrailsClient, trail_id: str) -> list[HistoryMessage]:
  """the conversation's projected prior exchanges, oldest first, collected
  across the fork ancestor chain. each ancestor contributes messages through
  its child's fork point.
  """
  target = client.get_trail(trail_id)
  segments = walk_header_chain(target, client.get_trail)
  messages: list[HistoryMessage] = []
  for header, bound in segments:
    step_id = bound['step_id'] if bound is not None else None
    messages.extend(_segment_messages(client, header['id'], step_id))
  return messages


def _segment_messages(
  client: TrailsClient, trail_id: str, up_to_step_id: Optional[int]
) -> list[HistoryMessage]:
  messages: list[HistoryMessage] = []
  for event in client.iter_messages(trail_id, types={'user_input', 'assistant'}):
    source_step_id = event['source']['step_id']
    if up_to_step_id is not None and source_step_id > up_to_step_id:
      break
    event_type = event.get('type')
    if event_type == 'user_input':
      messages.append(_message(client, event, by_user=True))
    elif event_type == 'assistant' and event.get('terminal') is True:
      messages.append(_message(client, event, by_user=False))
  return messages


def _message(client: TrailsClient, event: dict, *, by_user: bool) -> HistoryMessage:
  return HistoryMessage(
    by_user=by_user,
    text=client.resolve_body(event.get('content')),
    when=datetime.fromisoformat(event['ts']).astimezone(),
  )


def resume(
  client: TrailsClient,
  bro_name: str,
  trail_ref: str,
  *,
  llm_spec: LLMSpec,
  at: Optional[int] = None,
) -> ResumedCall:
  """continue a recorded `call` conversation: `trail_ref` is a trail id, or
  `RESUME_LATEST` for the bro's newest one. `at` names an explicit fork step
  instead of the trail's latest consistent point. raises `ValueError` when
  there is nothing to resume, or when the trail belongs to a different bro
  (the run is scoped — credentials, registry class — to the named one).
  """
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
