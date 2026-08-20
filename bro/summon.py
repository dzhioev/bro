#!/usr/bin/env python
"""summon — request another bro on the session's broker channel and get its answer.

The peer side of the summon mechanism: a `summon{target, prompt, timeout?, into?,
hold?, grant?, revoke?, llm?, harness?}`
request on the session channel, answered by the host-side handler (`ride/ride/summon_control.py`)
with `started{trail_id}` and exactly one terminal (`completed` / `failed` /
`reply{error}`). This module owns the request's wire contract — the type tag, the
payload keys, the 1800s default timeout — for all its consumers: the self-contained `summon` CLI, the bro service tools (`summon` /
`summon_check`, over the library functions `summon_and_wait`, `summon_detached`,
`check_summon`, `collect_summon`), and the blocking CLI relay helper
`relay_summon`.

`grant` / `revoke` are lists of scope overrides for the child, each value a
credential name or `@bro` for a summon target of its own; the host bounds a grant
by the sender's own scope, so a name it does not hold itself comes back denied.
`harness` names the driving loop the child runs under — `bro` (the default: the
target's own LLM process) or `claude` (a one-shot managed Claude Code session) —
and `llm` is the canonical `provider:model:effort+fast` recipe the child runs
within it (`bro.llm.providers`); a recipe the named harness cannot run fails the
spawn rather than switching the harness.

`manual: true` makes the request a *manual summon*: the host spawns nothing and
instead provisions a channel for a child the user launches themselves — the
request id doubles as the token the user's `ride along --summoned <token>`
launch consumes (`manual_launch_command` renders the exact command to relay).
The host acknowledges the registration with an interim `accepted` once the
token is live, and the manual client waits for it (`ACCEPT_TIMEOUT`), so a
denial fails at the summon — never later, after a dead token was already handed
to the user. The child then attaches as a regular summon peer, so the
check/collect flow is unchanged; the natural shape is `--detach` + polling,
since the launch is paced by a human. A manual request refuses `timeout`,
`hold`, `llm`, and `harness` — the user's launch owns the session's shape, and
there is no host-killable child for a timeout to bound.

Blocking mode sends, prints the request id and the `started` trail id to stderr,
and relays the terminal: the answer on stdout (exit 0), everything else as a
`SummonError` on stderr (exit 1) — a `completed` whose `end_reason` is `raised` or
`error` counts as failure. `--detach` prints the request id on stdout and exits
right after the send.

Any summon is reclaimable by the request id both modes print — detached or a
blocking wait that was killed mid-flight. `summon check <request-id>` peeks
without blocking: the answer when an unread result is in, `still running` on
stderr with exit 3 while it hasn't landed, exit 1 on an id the broxy doesn't
know. The peek rides the broxy's non-marking `check`, so it disturbs neither the
retained result nor a live waiter — safe to poll a backgrounded summon. `summon
check --wait <request-id>` collects for real (the broxy's `claim`): it blocks
like the original call did; the wait is a lock, so while another waiter is alive
it fails fast instead of stealing the result from under it, and once a result
was collected a further --wait errors. The broxy retains delivered conversations,
so a collected result is still readable: `summon check <request-id> --last-seen N`
is the cursor read — it replays the conversation from sequence N (0 = the start)
regardless of read status, the recovery path when the result was read by a wait
whose reply never arrived (an abandoned MCP tool call, a killed collect); a
`last_seen` ahead of what was actually read fails with "from the future". Every
mode rides the session broxy
— a set `BROKER_CHANNEL` always names one. Waits bound silence, not the run: the
deadline opens at max(effective timeout, `LAUNCH_TIMEOUT`) — the prepare phase
(image build, worktree seeding) runs before the host arms its request-lifecycle
timer, so only the backstop bounds it, and a claim that never sees a `started`
(consumed by the dead waiter) still gets the full run bound — and each correlated
`started` re-arms it to the effective timeout exactly: the host timer starts before
the client receives `started`, so the re-armed bound structurally outlives the host
backstop. The backstop normally delivers a terminal; expiry means it was lost (e.g.
sent while the broxy was down) or the launch wedged, and the failure message names
which phase went silent — no `started` points at the session's checkout-keyed
summon status/audit, while a lost terminal after `started` points at bro.trails.
`summon list` (`list_summons`) reads the session's summon-status file
(`RIDE_SUMMON_STATUS`, written host-side by `ride/ride/summon_control.py`) and reports the active
summons and the last finished one, each with its request id — the rediscovery
surface when a request id was lost with a dead client. `summon watch`
(`watch_summons`) follows the same file instead of sampling it, printing a line
as each child starts and ends: the shape a session's event-stream tool consumes,
so a summoner learns a child landed without spending a turn to ask. A run's own effective
allow-list travels the same way, in `RIDE_MAY_SUMMON`: the surface that launches a
run writes the list the host will authorize its summons against, and
`may_summon` reads it back, so a target's standing is readable instead of
discoverable by denial.

Unlike the substrate `broker` CLI, an unset `BROKER_CHANNEL` is an error, not
inert — a summon that silently does nothing is a failure. Broker imports are
deferred to call time so importers of the module-level constants (`ride/ride/summon_control.py`,
on the pre-gate launch path) never pull the broker package in.
"""

import contextlib
import json
import os
import time
from collections.abc import Callable, Collection, Generator
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Optional

import bro.base.args as base_args
from bro import summon_status
from bro.base import log
from bro.launch.llm_flags import (
  EFFORT_HELP,
  FAST_HELP,
  add_llm_flags,
  canonicalize,
  selection_from_args,
)
from bro.llm.providers import LLMSelectionError
from bro.mcp import HOLDS

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.client import Client

__cli_name__ = 'summon'

SUMMON = 'summon'  # the request's message-type tag (a consumer tag; not in broker's Tag)
SUMMONER_ENV = 'RIDE_SUMMONER'
# marks a run as a summoned child — the fact the claude solo runner keys its
# run-lifecycle emission on (a bro-run child emits from `BaseBro.run` instead)
SUMMONED_ENV = 'RIDE_SUMMONED'
# carries a run's own effective summon allow-list into it, written by the surface
# that launches the run: a session root's at launch, a summoned child's at its spawn
MAY_SUMMON_ENV = 'RIDE_MAY_SUMMON'
# the harness a request naming none runs its child under
DEFAULT_HARNESS = 'bro'
# request-lifecycle bound for a summoned child — sized so the flagship deploy
# workload survives the default; the substrate's generic 600s default is untouched
DEFAULT_TIMEOUT = 1800.0
# pre-`started` bound on a client wait — a backstop for a wedged prepare phase or a
# lost `started`, not a startup allowance: any real image rebuild + boot finishes
# far inside it. Independent of the request timeout; once `started` arrives the
# wait re-arms to the effective timeout (see the module docstring)
LAUNCH_TIMEOUT = 1800.0
# a check is answered by the session broxy locally and immediately — this bound only
# turns a wedged or unanswering broxy into a clean failure instead of a hang
CHECK_TIMEOUT = 10.0
# a manual summon is acknowledged (accepted or denied) by the host handler as soon
# as it provisions the channel — this bound only turns a wedged host into a clean
# failure instead of a hang
ACCEPT_TIMEOUT = 30.0
# `summon check` exit code while the result is not in yet (0 = answer relayed,
# 1 = failure, 2 = argparse usage error)
PENDING_EXIT_CODE = 3
HOLD_HELP = "the child's user-involvement level; omitted lets the child use its unattended default"
MANUAL_HELP = (
  'register a manual summon instead of spawning: the request id becomes the token '
  'a user-launched `ride along --summoned <token>` session answers'
)
HARNESS_HELP = (
  f"the harness the child runs under: '{DEFAULT_HARNESS}' (default — the target's own LLM "
  "process) or 'claude' (a one-shot managed Claude Code session)"
)
GRANT_HELP = "add a credential (NAME) or summonable bro (@BRO) to the child's scope (repeatable)"
REVOKE_HELP = (
  "remove a credential (NAME) or summonable bro (@BRO) from the child's scope (repeatable)"
)
INTO_HELP = "base the child's workspace on this git ref instead of the summoner's workspace HEAD"
DETACH_HELP = 'print the request id and exit after sending; collect it with summon check'


def manual_launch_command(request_id: str, target: str) -> str:
  """the ride command that launches a manual summon's child session — what the
  summoner relays to the user along with the token (the request id)."""
  return f'ride along --summoned {request_id} {target}'


def encode_may_summon(targets: Collection[str]) -> str:
  """an effective summon allow-list as the `MAY_SUMMON_ENV` value: the names
  sorted and comma-joined, empty for a run that may summon nothing."""
  return ','.join(sorted(set(targets)))


def may_summon() -> Optional[tuple[str, ...]]:
  """the bros this run may summon, as its launch fixed them — the empty tuple
  when it may summon none, and None when it was launched by a surface that
  publishes no list. Read-only: the host authorizes against its own copy, so
  nothing here can widen it."""
  raw = os.environ.get(MAY_SUMMON_ENV)
  if raw is None:
    return None
  return tuple(name for name in raw.split(',') if len(name) > 0)


def summoned_by_from_env() -> Optional[dict[str, Any]]:
  """this run's `summoned_by` trail provenance, read off `SUMMONER_ENV` — None
  when the run was not summoned or its summoner published no trail yet.

  Consumed on read: tool subprocesses inherit this process's environment, so a
  nested in-process run inside the summoned child's container must not re-stamp
  the parent's summoned_by on its own trail — it was not itself summoned."""
  raw = os.environ.pop(SUMMONER_ENV, None)
  if raw is None:
    return None
  summoned_by = json.loads(raw)
  if not isinstance(summoned_by, dict):
    raise ValueError(f'{SUMMONER_ENV} must be a JSON object')
  keys = set(summoned_by)
  if keys == {'session'} and isinstance(summoned_by['session'], str):
    return None
  if keys == {'target', 'trail_id'} and all(isinstance(summoned_by[key], str) for key in keys):
    return {'trail_id': summoned_by['trail_id']}
  if not {'trail_id'}.issubset(keys) or not keys.issubset({'trail_id', 'step_id', 'index'}):
    raise ValueError(f'{SUMMONER_ENV} has an invalid summoned_by shape')
  trail_id = summoned_by['trail_id']
  step_id = summoned_by.get('step_id')
  index = summoned_by.get('index')
  if (
    not isinstance(trail_id, str)
    or len(trail_id) == 0
    or (
      step_id is not None
      and (not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 0)
    )
    or (
      index is not None
      and (step_id is None or not isinstance(index, int) or isinstance(index, bool) or index < 0)
    )
  ):
    raise ValueError(f'{SUMMONER_ENV} has an invalid summoned_by shape')
  return summoned_by


class SummonError(Exception):
  """a summon that produced no usable answer: denied, malformed, raised, failed,
  or its terminal never arrived. The message is the operator-facing reason."""


def _open_client() -> 'Client':
  from bro.broker.client import CHANNEL_ENV, Client

  client = Client.from_env()
  if client is None:
    raise SummonError(f'no broker channel ({CHANNEL_ENV} unset); summon needs a session channel')
  return client


def _payload(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  manual: bool = False,
) -> dict[str, Any]:
  payload: dict[str, Any] = {'target': target, 'prompt': prompt}
  if timeout is not None:
    payload['timeout'] = timeout
  if into is not None:
    payload['into'] = into
  if hold is not None:
    payload['hold'] = hold
  if step_id is not None:
    payload['step_id'] = step_id
  if index is not None:
    payload['index'] = index
  if grant is not None:
    payload['grant'] = list(grant)
  if revoke is not None:
    payload['revoke'] = list(revoke)
  if llm is not None:
    payload['llm'] = llm
  if harness is not None:
    payload['harness'] = harness
  if manual:
    payload['manual'] = True
  return payload


@contextlib.contextmanager
def _connection(client: Optional['Client']) -> Generator['Client']:
  """the channel client a call runs on: a caller-owned one passed through with
  its lifecycle left alone, or a fresh one closed on the way out."""
  if client is not None:
    yield client
    return
  with _open_client() as owned:
    yield owned


def _trails_hint(trail_id: Optional[str]) -> str:
  if trail_id is not None:
    return f'inspect the run with `rewind show {trail_id}`'
  return 'look for the run with `rewind list`'


def _interpret_terminal(terminal: 'Message', trail_id: Optional[str]) -> str:
  """turn a summon terminal into the answer, or raise `SummonError` with the
  failure reason."""
  from bro.broker.brotocol import Tag

  if terminal.type == Tag.COMPLETED:
    end_reason = terminal.payload.get('end_reason')
    result = terminal.payload.get('result')
    if end_reason == 'ok':
      return result if result is not None else ''
    raise SummonError(f'summon {end_reason}: {result}')
  if terminal.type == Tag.FAILED:
    reason = terminal.payload.get('reason')
    detail = terminal.payload.get('error', terminal.payload.get('output_tail'))
    parts = [f'summon failed ({reason})']
    if detail is not None and len(str(detail).strip()) > 0:
      parts.append(str(detail).strip())
    parts.append(_trails_hint(trail_id))
    raise SummonError('; '.join(parts))
  if terminal.type == Tag.REPLY:
    raise SummonError(str(terminal.payload.get('error', terminal.payload)))
  raise SummonError(f'unexpected summon terminal {terminal.type!r}: {terminal.payload}')


def _await_answer(
  client: 'Client',
  request: 'Message',
  *,
  timeout: float,
  on_started: Optional[Callable[[str], None]] = None,
) -> str:
  """block for the request's terminal and interpret it; returns the answer or
  raises `SummonError` with the failure reason. The wait opens bounded at
  `max(timeout, LAUNCH_TIMEOUT)` and re-arms to `timeout` on each interim
  (see the module docstring)."""
  from bro.broker.brotocol import Tag

  trail_id: Optional[str] = None
  started_seen = False

  def _interim(message: 'Message') -> None:
    nonlocal trail_id, started_seen
    if message.type != Tag.STARTED:
      return
    started_seen = True
    trail_id = message.payload.get('trail_id')
    if on_started is not None and trail_id is not None:
      on_started(trail_id)

  launch_bound = max(timeout, LAUNCH_TIMEOUT)
  try:
    terminal = client.await_reply(
      request, launch_bound, on_interim=_interim, timeout_after_interim=timeout
    )
  except TimeoutError:
    if started_seen:
      raise SummonError(
        f'no summon terminal within {timeout:.0f}s of started — the result was lost '
        f'or the child is still running; {_trails_hint(trail_id)}'
      ) from None
    raise SummonError(
      f'no started and no terminal within {launch_bound:.0f}s — the child likely '
      'never launched; check the session summon status and checkout-keyed runtime audit'
    ) from None
  except ConnectionError as e:
    raise SummonError(f'broker channel closed awaiting the summon result: {e}') from None
  return _interpret_terminal(terminal, trail_id)


def open_client() -> 'Client':
  """a connected channel client for a caller that owns the lifecycle itself. The
  bro service tools open one outside their worker thread so a cancelled tool call
  can close it and unblock the blocking wait — the broxy sees the waiter go and
  the terminal buffers for a later check/collect (`ClientTransport.close`
  documents the cross-thread abort guarantee this rides on). Raises `SummonError`
  when the session has no channel."""
  return _open_client()


def summon_and_wait(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
  client: Optional['Client'] = None,
) -> str:
  """send one summon and block for the answer — the bro `summon` tool's default
  path. `step_id` and `index` name the summoner's projected tool call so the
  child's `summoned_by` carries the precise position. With `client` the caller owns the
  connection's lifecycle (closing it from another thread aborts the wait);
  without, a fresh one is opened and closed per call. Raises `SummonError` on
  any failure."""
  payload = _payload(
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    step_id=step_id,
    index=index,
    grant=grant,
    revoke=revoke,
    llm=llm,
    harness=harness,
  )
  with _connection(client) as connection:
    request = connection.send(SUMMON, payload)
    return _await_answer(
      connection, request, timeout=timeout if timeout is not None else DEFAULT_TIMEOUT
    )


def summon_detached(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
) -> str:
  """send one summon and return its request id without waiting — the bro `summon`
  tool's detach path. Collect with `collect_summon`, poll with `check_summon`."""
  payload = _payload(
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    step_id=step_id,
    index=index,
    grant=grant,
    revoke=revoke,
    llm=llm,
    harness=harness,
  )
  with _open_client() as client:
    return client.send(SUMMON, payload).id


def _await_acceptance(client: 'Client', request: 'Message') -> None:
  """block until the host acknowledges a manual summon with `accepted`; a denial
  or failed registration raises `SummonError` with the reason."""
  from bro.broker.brotocol import Tag

  try:
    first = client.await_any(request, ACCEPT_TIMEOUT)
  except TimeoutError:
    raise SummonError(
      f'no acknowledgment within {ACCEPT_TIMEOUT:.0f}s of the manual summon — '
      "check the session's summon audit and host log"
    ) from None
  except ConnectionError as e:
    raise SummonError(f'broker channel closed awaiting the manual summon acknowledgment: {e}') from None  # fmt: skip
  if first.type == Tag.ACCEPTED:
    return
  if first.type in (Tag.REPLY, Tag.FAILED):
    _interpret_terminal(first, None)  # denials and failed registrations raise here
  raise SummonError(f'unexpected manual summon acknowledgment {first.type!r}: {first.payload}')


def summon_manual(
  target: str,
  prompt: str,
  *,
  into: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  step_id: Optional[int] = None,
  index: Optional[int] = None,
) -> str:
  """register one manual summon and return its token (the request id) once the
  host acknowledges the registration — so a denial fails here, at the summon,
  never later at collection: the token a user is told to launch against is a
  token the host is already expecting. Poll or collect the token with
  `check_summon` / `collect_summon` like any detached summon. Raises
  `SummonError` on denial, a failed registration, or a host that never
  acknowledges."""
  payload = _payload(target, prompt, into=into, grant=grant, revoke=revoke, manual=True, step_id=step_id, index=index)  # fmt: skip
  with _open_client() as client:
    request = client.send(SUMMON, payload)
    _await_acceptance(client, request)
    return request.id


@dataclass(frozen=True)
class SummonStatus:
  """a `check_summon` outcome: the answer once a result was readable, `pending`
  while the child runs, or `collected` — the conversation ended and its result
  was already read (re-read it with `last_seen`). `seq` is the conversation's
  highest retained sequence; after a cursor read it is also the new cursor."""

  pending: bool
  answer: Optional[str] = None
  trail_id: Optional[str] = None
  collected: bool = False
  seq: Optional[int] = None


def check_summon(request_id: str, *, last_seen: Optional[int] = None) -> SummonStatus:
  """non-blocking check on a summon by request id (the broxy's `check`). Without
  `last_seen`: a non-marking peek — the answer when an unread result is in, else
  the pending/collected state. With `last_seen` (0 = the start): a cursor read —
  replays the conversation from that sequence regardless of read status, the
  recovery path when a result was read by a wait whose reply never arrived; the
  returned `seq` is the new cursor. Raises `SummonError` when the id is unknown,
  the summon failed, `last_seen` is ahead of what was read, or no broxy
  answers."""
  from bro.broker.brotocol import Tag

  payload: dict[str, Any] = {'id': request_id}
  if last_seen is not None:
    payload['last_seen'] = last_seen
  with _open_client() as client:
    check = client.send(Tag.CHECK, payload)
    trail_id: Optional[str] = None
    interim_count = 0

    def _interim(message: 'Message') -> None:
      nonlocal trail_id, interim_count
      interim_count += 1
      if message.payload.get('trail_id') is not None:
        trail_id = message.payload.get('trail_id')

    try:
      terminal = client.await_reply(check, CHECK_TIMEOUT, on_interim=_interim)
    except TimeoutError:
      raise SummonError(
        f'no check reply within {CHECK_TIMEOUT:.0f}s — the session broxy is not answering'
      ) from None
    except ConnectionError as e:
      raise SummonError(f'broker channel closed awaiting the check reply: {e}') from None
    # the broxy's own state replies carry 'state'; every real summon message
    # (completed / failed / the handler's reply{error}) does not
    state = terminal.payload.get('state')
    if state == 'pending' or state == 'collected':
      return SummonStatus(
        pending=state == 'pending',
        collected=state == 'collected',
        trail_id=terminal.payload.get('trail_id', trail_id),
        seq=terminal.payload.get('seq'),
      )
    if state == 'unknown':
      raise SummonError(
        f'unknown request id {request_id} (not sent through this session, or evicted); '
        f'{_trails_hint(None)}'
      )
    # a real terminal closed the reply: with a cursor, the contiguous window
    # gives each message's sequence by counting from last_seen
    seq = last_seen + interim_count + 1 if last_seen is not None else None
    return SummonStatus(
      pending=False, answer=_interpret_terminal(terminal, trail_id), trail_id=trail_id, seq=seq
    )


def collect_summon(
  request_id: str,
  *,
  timeout: Optional[float] = None,
  on_started: Optional[Callable[[str], None]] = None,
  client: Optional['Client'] = None,
) -> str:
  """claim a summon by request id and block for its answer (the broxy's `claim`).
  The wait is a lock: fails fast while another waiter is alive, and errors once
  the result was already collected — re-read that with `check_summon(last_seen=…)`.
  With `client` the caller owns the connection's lifecycle (closing it from
  another thread aborts the wait); without, a fresh one is opened and closed per
  call. Raises `SummonError` on any failure."""
  from bro.broker.brotocol import Tag

  with _connection(client) as connection:
    claim = connection.send(Tag.CLAIM, {'id': request_id})
    return _await_answer(
      connection,
      claim,
      timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
      on_started=on_started,
    )


def list_summons() -> dict[str, Any]:
  """the session's summons as the host recorded them: `{'active': [...], 'last': …}`
  from the status file `RIDE_SUMMON_STATUS` points at, each entry carrying its
  `request_id` — the reattach handle for `check_summon` / `collect_summon`. The
  host writes the file with the session's first summon; before that the state is
  empty. Raises `SummonError` when the environment carries no status file (only
  ride-launched sessions track summon status)."""
  path = summon_status.status_path()
  if path is None:
    raise SummonError(
      f'no summon status file ({summon_status.STATUS_ENV} unset); '
      'only managed ride sessions track summon status'
    )
  return asdict(summon_status.read(path))


_WATCH_POLL_SECONDS = 1.0


def watch_summons(poll_seconds: float = _WATCH_POLL_SECONDS) -> Generator[str]:
  """the session's summon lifecycle, a line per event, until the caller stops
  reading.

  Reports each child starting (with the trail id to read it by) and each one
  ending. What is already in flight when the call is made is the baseline rather
  than an event, so a watch reports from where it was armed. Same status file as
  `list_summons`, and the same requirement of a ride-launched session.
  """
  path = summon_status.status_path()
  if path is None:
    raise SummonError(
      f'no summon status file ({summon_status.STATUS_ENV} unset); '
      'only ride-launched sessions track summon status'
    )
  watched = {entry.request_id: entry for entry in summon_status.read(path).active}
  while True:
    time.sleep(poll_seconds)
    status = summon_status.read(path)
    active = {entry.request_id: entry for entry in status.active}
    for request_id, entry in active.items():
      previous = watched.get(request_id)
      if entry.trail_id is not None and (previous is None or previous.trail_id is None):
        yield f'{entry.target} started — trail {entry.trail_id} (request {request_id})'
    for request_id, entry in watched.items():
      if request_id in active:
        continue
      # the status file retains one finished summon, so a second one ending
      # within the same poll leaves the earlier outcome unreadable — its end is
      # still reported, unnamed, and `summon check` has the answer either way
      last = status.last
      outcome = last.outcome if last is not None and last.request_id == request_id else 'ended'
      yield f'{entry.target} {outcome} (request {request_id})'
    watched = active


def relay_summon(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  llm: Optional[str] = None,
  harness: Optional[str] = None,
  manual: bool = False,
) -> int:
  """send one summon and relay its outcome as a CLI would: the request id and
  the started trail id to stderr, the answer to stdout, any failure as an error
  log line. Returns the exit code — the blocking `summon` CLI mode, exposed for
  the self-contained blocking `summon` CLI. A blocking manual summon prints the
  launch command to relay once the host accepts (a denial fails right there);
  note the wait after acceptance is still bounded like any blocking summon, so a
  user slower than `LAUNCH_TIMEOUT` is better served by `--detach`."""
  payload = _payload(
    target,
    prompt,
    timeout=timeout,
    into=into,
    hold=hold,
    grant=grant,
    revoke=revoke,
    llm=llm,
    harness=harness,
    manual=manual,
  )
  try:
    client = _open_client()
  except SummonError as e:
    log.error('%s', e)
    return 1
  with client:
    request = client.send(SUMMON, payload)
    log.info('summon request %s', request.id)
    if manual:
      try:
        _await_acceptance(client, request)
      except SummonError as e:
        log.error('%s', e)
        return 1
      log.info('have the user run: %s', manual_launch_command(request.id, target))
    effective = timeout if timeout is not None else DEFAULT_TIMEOUT
    return _relay(
      lambda: _await_answer(
        client,
        request,
        timeout=effective,
        on_started=lambda trail_id: log.info('summon started: trail %s', trail_id),
      )
    )


# --- CLI ------------------------------------------------------------------------


def _relay(await_answer: Callable[[], str]) -> int:
  try:
    result = await_answer()
  except SummonError as e:
    log.error('%s', e)
    return 1
  print(result)
  return 0


def _list() -> int:
  try:
    status = list_summons()
  except SummonError as e:
    log.error('%s', e)
    return 1
  print(json.dumps(status, indent=2, ensure_ascii=False))
  return 0


def _watch() -> int:
  try:
    for event in watch_summons():
      print(event, flush=True)
  except SummonError as e:
    log.error('%s', e)
    return 1
  return 0


def _check(request_id: str, wait: bool, timeout: Optional[float], last_seen: Optional[int]) -> int:
  if wait and last_seen is not None:
    log.error('--last-seen is a cursor read; it does not combine with --wait')
    return 1
  if timeout is not None and not wait:
    log.error('--timeout only bounds a --wait; a plain check never blocks')
    return 1
  if wait:
    return _relay(
      lambda: collect_summon(
        request_id,
        timeout=timeout,
        on_started=lambda trail_id: log.info('summon started: trail %s', trail_id),
      )
    )
  try:
    status = check_summon(request_id, last_seen=last_seen)
  except SummonError as e:
    log.error('%s', e)
    return 1
  if status.pending:
    log.info('summon still running; %s', _trails_hint(status.trail_id))
    return PENDING_EXIT_CODE
  if status.answer is None:  # collected: the conversation ended, its result was read
    through = f' (read through seq {status.seq})' if status.seq is not None else ''
    log.info(
      'summon result was already read%s; re-read the conversation with '
      '`summon check %s --last-seen 0`',
      through,
      request_id,
    )
    return 1
  print(status.answer)
  return 0


def main(argv: list[str]) -> Optional[int]:
  if len(argv) > 1 and argv[1] == 'list':
    parser = base_args.Parser(
      prog='summon list',
      description="list this session's summons as the host recorded them: the "
      'active ones and the last finished one, each with the request id — the '
      'reattach handle for `summon check`',
    )
    return _list(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'watch':
    parser = base_args.Parser(
      prog='summon watch',
      description="stream this session's summon lifecycle: a line when a child "
      'starts, naming its trail, and a line when one ends, naming its outcome. '
      'Runs until killed; what is already in flight when it starts is the '
      'baseline rather than an event',
    )
    return _watch(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'check':
    parser = base_args.Parser(
      prog='summon check',
      description='check on a detached or interrupted summon by its request id: '
      'print the answer if the result is in, otherwise report `still running` and '
      f'exit {PENDING_EXIT_CODE} without blocking; --wait blocks and collects instead',
    )
    parser.add_argument('request_id', help='request id printed by the original summon')
    parser.add_argument(
      '--wait',
      action='store_true',
      help='block until the result arrives and consume it; errors right away when '
      'another process is already waiting on the id (a plain check leaves the '
      'result in place and disturbs no concurrent waiter)',
    )
    parser.add_argument(
      '--timeout',
      type=float,
      help='with --wait: seconds the summon was given (bounds the wait from the '
      "child's start; default: the summon default)",
    )
    parser.add_argument(
      '--last-seen',
      type=int,
      help='cursor read: replay the conversation from this sequence (0 = the '
      'start) regardless of read status — recovers a result that was already '
      'read by a dead wait; not combinable with --wait',
    )
    return _check(**parser.parse(argv[1:]))
  parser = base_args.Parser(
    prog='summon',
    description='summon a bro over the session channel; use `summon check` to reattach '
    'to a request and `summon list` to rediscover request ids',
  )
  parser.add_argument('target', help='bro to summon')
  parser.add_argument('prompt', help='request the summoned bro answers')
  add_llm_flags(parser, effort_help=EFFORT_HELP, fast_help=FAST_HELP)
  parser.add_argument('--grant', action='append', default=None, metavar='NAME', help=GRANT_HELP)
  parser.add_argument('--revoke', action='append', default=None, metavar='NAME', help=REVOKE_HELP)
  parser.add_argument('--into', metavar='REF', help=INTO_HELP)
  parser.add_argument('--hold', choices=HOLDS, default=None, help=HOLD_HELP)
  parser.add_argument('--harness', default=None, help=HARNESS_HELP)
  parser.add_argument(
    '--timeout',
    type=float,
    metavar='SECONDS',
    help=f'seconds before the host kills the child (default: {DEFAULT_TIMEOUT:.0f})',
  )
  parser.add_argument('--manual', action='store_true', help=MANUAL_HELP)
  parser.add_argument('--detach', action='store_true', help=DETACH_HELP)
  args = parser.parse(argv)
  try:
    canonicalize(args, selection_from_args(args))
  except LLMSelectionError as error:
    log.error('%s', error)
    return 1
  if args['manual']:
    launch_owned = {
      '--timeout': args['timeout'],
      '--hold': args['hold'],
      '--harness': args['harness'],
      '--llm': args['llm'],
    }
    passed = sorted(flag for flag, value in launch_owned.items() if value is not None)
    if len(passed) > 0:
      log.error("a manual summon's launch owns %s; drop the flag(s)", ', '.join(passed))
      return 1
  os.environ.setdefault('BRO_SHELL_COMMAND', ' '.join(parser.reconstruct(args, prog=['summon'])))
  if args['detach']:
    try:
      if args['manual']:
        request_id = summon_manual(
          args['target'],
          args['prompt'],
          into=args['into'],
          grant=args['grant'],
          revoke=args['revoke'],
        )
        log.info('have the user run: %s', manual_launch_command(request_id, args['target']))
      else:
        request_id = summon_detached(
          args['target'],
          args['prompt'],
          timeout=args['timeout'],
          into=args['into'],
          hold=args['hold'],
          grant=args['grant'],
          revoke=args['revoke'],
          llm=args['llm'],
          harness=args['harness'],
        )
    except SummonError as error:
      log.error('%s', error)
      return 1
    print(request_id)
    return 0
  return relay_summon(
    args['target'],
    args['prompt'],
    timeout=args['timeout'],
    into=args['into'],
    hold=args['hold'],
    grant=args['grant'],
    revoke=args['revoke'],
    llm=args['llm'],
    harness=args['harness'],
    manual=args['manual'],
  )
