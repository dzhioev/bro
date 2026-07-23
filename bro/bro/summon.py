#!/usr/bin/env python
"""summon — request another bro on the session's broker channel and get its answer.

The peer side of the summon mechanism: a `summon{target, prompt, timeout?, into?, hold?}`
request on the session channel, answered by the host-side handler (`bro/launch/summon_control.py`)
with `started{trail_id}` and exactly one terminal (`completed` / `failed` /
`reply{error}`). This module owns the request's wire contract — the type tag, the
payload keys, the 1800s default timeout — for all its consumers: `bro run
--summon` and its bare `summon` alias, the bro service tools (`summon` /
`summon_check`, over the library functions `summon_and_wait`, `summon_detached`,
`check_summon`, `collect_summon`), and the blocking CLI relay helper
`relay_summon`.

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
which phase went silent — no `started` points at the session's summon status/audit
(`var/cw/summon/`), a lost terminal after `started` points at trails.

`summon list` (`list_summons`) reads the session's summon-status file
(`CW_SUMMON_STATUS`, written host-side by `bro/launch/summon_control.py`) and reports the active
summons and the last finished one, each with its request id — the rediscovery
surface when a request id was lost with a dead client.

Unlike the substrate `broker` CLI, an unset `BROKER_CHANNEL` is an error, not
inert — a summon that silently does nothing is a failure. Broker imports are
deferred to call time so importers of the module-level constants (`bro/launch/summon_control.py`,
on the pre-gate launch path) never pull the broker package in.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import base.args
from base import log

if TYPE_CHECKING:
  from broker.brotocol import Message
  from broker.client import Client

__cli_name__ = 'summon'

SUMMON = 'summon'  # the request's message-type tag (a consumer tag; not in broker's Tag)
# points a session at its summon-status file — set by the launch surfaces
# (bro/launch/summon_control.py owns the writer), read by `session-log.statusline`
STATUS_ENV = 'CW_SUMMON_STATUS'
SUMMONER_ENV = 'CW_SUMMONER'
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
# `summon check` exit code while the result is not in yet (0 = answer relayed,
# 1 = failure, 2 = argparse usage error)
PENDING_EXIT_CODE = 3


class SummonError(Exception):
  """a summon that produced no usable answer: denied, malformed, raised, failed,
  or its terminal never arrived. The message is the operator-facing reason."""


def _open_client() -> 'Client':
  from broker.client import CHANNEL_ENV, Client

  client = Client.from_env()
  if client is None:
    raise SummonError(f'no broker channel ({CHANNEL_ENV} unset); summon needs a session channel')
  return client


def _payload(
  target: str, prompt: str, timeout: Optional[float], into: Optional[str], hold: Optional[str]
) -> dict[str, Any]:
  payload: dict[str, Any] = {'target': target, 'prompt': prompt}
  if timeout is not None:
    payload['timeout'] = timeout
  if into is not None:
    payload['into'] = into
  if hold is not None:
    payload['hold'] = hold
  return payload


def _trails_hint(trail_id: Optional[str]) -> str:
  if trail_id is not None:
    return f'inspect the run with `trails show {trail_id}`'
  return 'look for the run with `trails list`'


def _interpret_terminal(terminal: 'Message', trail_id: Optional[str]) -> str:
  """turn a summon terminal into the answer, or raise `SummonError` with the
  failure reason."""
  from broker.brotocol import Tag

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
  `max(timeout, LAUNCH_TIMEOUT)` and re-arms to `timeout` on each `started`
  (see the module docstring)."""
  trail_id: Optional[str] = None
  started_seen = False

  def _started(message: 'Message') -> None:
    nonlocal trail_id, started_seen
    started_seen = True
    trail_id = message.payload.get('trail_id')
    if on_started is not None and trail_id is not None:
      on_started(trail_id)

  launch_bound = max(timeout, LAUNCH_TIMEOUT)
  try:
    terminal = client.await_reply(
      request, launch_bound, on_started=_started, timeout_after_started=timeout
    )
  except TimeoutError:
    if started_seen:
      raise SummonError(
        f'no summon terminal within {timeout:.0f}s of started — the result was lost '
        f'or the child is still running; {_trails_hint(trail_id)}'
      ) from None
    raise SummonError(
      f'no started and no terminal within {launch_bound:.0f}s — the child likely '
      f'never launched; check the session summon status and audit (var/cw/summon/)'
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
  client: Optional['Client'] = None,
) -> str:
  """send one summon and block for the answer — the bro `summon` tool's default
  path. With `client` the caller owns the connection's lifecycle (closing it from
  another thread aborts the wait); without, a fresh one is opened and closed per
  call. Raises `SummonError` on any failure."""
  if client is None:
    with _open_client() as owned:
      return summon_and_wait(target, prompt, timeout=timeout, into=into, hold=hold, client=owned)
  request = client.send(SUMMON, _payload(target, prompt, timeout, into, hold))
  return _await_answer(client, request, timeout=timeout if timeout is not None else DEFAULT_TIMEOUT)


def summon_detached(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
) -> str:
  """send one summon and return its request id without waiting — the bro `summon`
  tool's detach path. Collect with `collect_summon`, poll with `check_summon`."""
  with _open_client() as client:
    return client.send(SUMMON, _payload(target, prompt, timeout, into, hold)).id


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
  from broker.brotocol import Tag

  payload: dict[str, Any] = {'id': request_id}
  if last_seen is not None:
    payload['last_seen'] = last_seen
  with _open_client() as client:
    check = client.send(Tag.CHECK, payload)
    trail_id: Optional[str] = None
    interim_count = 0

    def _started(message: 'Message') -> None:
      nonlocal trail_id, interim_count
      interim_count += 1
      if message.payload.get('trail_id') is not None:
        trail_id = message.payload.get('trail_id')

    try:
      terminal = client.await_reply(check, CHECK_TIMEOUT, on_started=_started)
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
  from broker.brotocol import Tag

  if client is None:
    with _open_client() as owned:
      return collect_summon(request_id, timeout=timeout, on_started=on_started, client=owned)
  claim = client.send(Tag.CLAIM, {'id': request_id})
  return _await_answer(
    client,
    claim,
    timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
    on_started=on_started,
  )


def list_summons() -> dict[str, Any]:
  """the session's summons as the host recorded them: `{'active': [...], 'last': …}`
  from the status file `CW_SUMMON_STATUS` points at, each entry carrying its
  `request_id` — the reattach handle for `check_summon` / `collect_summon`. The
  host writes the file with the session's first summon; before that the state is
  empty. Raises `SummonError` when the environment carries no status file (only
  cw-launched sessions track summon status)."""
  status_path = os.environ.get(STATUS_ENV)
  if status_path is None:
    raise SummonError(
      f'no summon status file ({STATUS_ENV} unset); only cw-launched sessions track summon status'
    )
  try:
    raw = Path(status_path).read_text()
  except FileNotFoundError:
    return {'active': [], 'last': None}  # no summon ever ran in this session
  return json.loads(raw)


def relay_summon(
  target: str,
  prompt: str,
  *,
  timeout: Optional[float] = None,
  into: Optional[str] = None,
  hold: Optional[str] = None,
) -> int:
  """send one summon and relay its outcome as a CLI would: the request id and
  the started trail id to stderr, the answer to stdout, any failure as an error
  log line. Returns the exit code — the blocking `summon` CLI mode, exposed for
  surfaces that relay a whole run through the host (`bro run --summon` and its
  aliases)."""
  try:
    client = _open_client()
  except SummonError as e:
    log.error('%s', e)
    return 1
  with client:
    request = client.send(SUMMON, _payload(target, prompt, timeout, into, hold))
    log.info('summon request %s', request.id)
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
    parser = base.args.Parser(
      prog='summon list',
      description="list this session's summons as the host recorded them: the "
      'active ones and the last finished one, each with the request id — the '
      'reattach handle for `summon check`',
    )
    return _list(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'check':
    parser = base.args.Parser(
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
  from bro.launch._cli import run_main

  return run_main(
    argv,
    program=['summon'],
    description='summon a bro over the session channel; use `summon check` to reattach '
    'to a request and `summon list` to rediscover request ids',
    force_summon=True,
  )
