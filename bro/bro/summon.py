#!/usr/bin/env python
"""summon — request another bro on the session's broker channel and get its answer.

The peer side of the summon mechanism: a `summon{target, prompt, timeout?, into?}`
request on the session channel, answered by the host-side handler (`cw/summon.py`)
with `started{trail_id}` and exactly one terminal (`completed` / `failed` /
`reply{error}`). This module owns the request's wire contract — the type tag, the
payload keys, the 1800s default timeout — for both its consumers: the `summon`
console script (blocking / `--detach` / `check` modes) and the bro service tools
(`summon` / `summon_check`, over the library functions `summon_and_wait`,
`summon_detached`, `check_summon`, `collect_summon`).

Blocking mode sends, prints the request id and the `started` trail id to stderr,
and relays the terminal: the answer on stdout (exit 0), everything else as a
`SummonError` on stderr (exit 1) — a `completed` whose `end_reason` is `raised` or
`error` counts as failure. `--detach` prints the request id on stdout and exits
right after the send.

Any summon is reclaimable by the request id both modes print — detached or a
blocking wait that was killed mid-flight. `summon check <request-id>` peeks
without blocking: the answer when the result already landed, `still running` on
stderr with exit 3 while it hasn't, exit 1 on an id the broxy doesn't know. The
peek rides the broxy's non-consuming `check`, so it disturbs neither the buffered
result nor a live waiter — safe to poll a backgrounded summon. `summon check
--wait <request-id>` collects for real (the broxy's `claim`): it blocks like the
original call and consumes the result; the wait is a lock, so while another
waiter is alive it fails fast instead of stealing the result from under it —
only a killed or detached wait is collectable. Every mode rides the session broxy
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

Unlike the substrate `broker` CLI, an unset `BROKER_CHANNEL` is an error, not
inert — a summon that silently does nothing is a failure. Broker imports are
deferred to call time so importers of the module-level constants (`cw/summon.py`,
on the pre-gate launch path) never pull the broker package in.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import base.args
from base import log

if TYPE_CHECKING:
  from broker.brotocol import Message
  from broker.client import Client

__cli_name__ = 'summon'

SUMMON = 'summon'  # the request's message-type tag (a consumer tag; not in broker's Tag)
# points a session at its summon-status file — set by the launch surfaces
# (cw/summon.py owns the writer), read by `session-log-statusline`
STATUS_ENV = 'CW_SUMMON_STATUS'
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
  target: str, prompt: str, timeout: Optional[float], into: Optional[str]
) -> dict[str, Any]:
  payload: dict[str, Any] = {'target': target, 'prompt': prompt}
  if timeout is not None:
    payload['timeout'] = timeout
  if into is not None:
    payload['into'] = into
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
    if end_reason == 'terminal':
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


def summon_and_wait(
  target: str, prompt: str, *, timeout: Optional[float] = None, into: Optional[str] = None
) -> str:
  """send one summon over a fresh channel client and block for the answer — the
  bro `summon` tool's default path. Raises `SummonError` on any failure."""
  with _open_client() as client:
    request = client.send(SUMMON, _payload(target, prompt, timeout, into))
    return _await_answer(
      client, request, timeout=timeout if timeout is not None else DEFAULT_TIMEOUT
    )


def summon_detached(
  target: str, prompt: str, *, timeout: Optional[float] = None, into: Optional[str] = None
) -> str:
  """send one summon and return its request id without waiting — the bro `summon`
  tool's detach path. Collect with `collect_summon`, poll with `check_summon`."""
  with _open_client() as client:
    return client.send(SUMMON, _payload(target, prompt, timeout, into)).id


@dataclass(frozen=True)
class SummonStatus:
  """a `check_summon` outcome: the answer once the result is in, else pending."""

  pending: bool
  answer: Optional[str] = None
  trail_id: Optional[str] = None


def check_summon(request_id: str) -> SummonStatus:
  """non-blocking, non-consuming peek at a summon by request id (the broxy's
  `check`). Returns the status; raises `SummonError` when the id is unknown or
  already consumed, when the summon failed, or when no broxy answers."""
  from broker.brotocol import Tag

  with _open_client() as client:
    check = client.send(Tag.CHECK, {'id': request_id})
    trail_id: Optional[str] = None

    def _started(message: 'Message') -> None:
      nonlocal trail_id
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
    if state == 'pending':
      return SummonStatus(pending=True, trail_id=terminal.payload.get('trail_id'))
    if state == 'unknown':
      raise SummonError(
        f'unknown or already-consumed request id {request_id}; {_trails_hint(None)}'
      )
    return SummonStatus(
      pending=False, answer=_interpret_terminal(terminal, trail_id), trail_id=trail_id
    )


def collect_summon(
  request_id: str,
  *,
  timeout: Optional[float] = None,
  on_started: Optional[Callable[[str], None]] = None,
) -> str:
  """claim a summon by request id and block for its answer (the broxy's `claim`,
  consuming). The wait is a lock: fails fast while another waiter is alive; a
  killed or detached wait is collectable. Raises `SummonError` on any failure."""
  from broker.brotocol import Tag

  with _open_client() as client:
    claim = client.send(Tag.CLAIM, {'id': request_id})
    return _await_answer(
      client,
      claim,
      timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
      on_started=on_started,
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


def _summon(
  target: str, prompt: str, timeout: Optional[float], into: Optional[str], detach: bool
) -> int:
  try:
    client = _open_client()
  except SummonError as e:
    log.error('%s', e)
    return 1
  with client:
    request = client.send(SUMMON, _payload(target, prompt, timeout, into))
    if detach:
      print(request.id)
      return 0
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


def _check(request_id: str, wait: bool, timeout: Optional[float]) -> int:
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
    status = check_summon(request_id)
  except SummonError as e:
    log.error('%s', e)
    return 1
  if status.pending:
    log.info('summon still running; %s', _trails_hint(status.trail_id))
    return PENDING_EXIT_CODE
  print(status.answer)
  return 0


def main(argv: list[str]) -> Optional[int]:
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
    return _check(**parser.parse(argv[1:]))
  parser = base.args.Parser(
    description='summon a bro over the session broker channel and print its answer; '
    '`summon check <request-id>` checks on a detached or interrupted summon'
  )
  parser.add_argument('target', help='bro to summon (must be in the session summon allow-list)')
  parser.add_argument('prompt', help='the request the summoned bro answers')
  parser.add_argument(
    '--timeout',
    type=float,
    help=f'seconds before the host kills the child (default: {DEFAULT_TIMEOUT:.0f}; '
    'an open-ended run — e.g. a dev child watching a PR through review — outlives '
    'the default and needs hours)',
  )
  parser.add_argument(
    '--into', help="base the child on this git ref instead of this workspace's current HEAD"
  )
  parser.add_argument(
    '--detach',
    action='store_true',
    help='print the request id on stdout and exit right after the send; '
    'collect the result later with `summon check`',
  )
  return _summon(**parser.parse(argv))
