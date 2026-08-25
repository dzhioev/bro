#!/usr/bin/env python
"""benchmark-job — run this checkout's benchmark jobs through the session broker.

The `benchmark` kind lets a managed session start a benchmark run without
holding any docker authority of its own: the session sends one coarse request
over its broker channel, and the host — where the docker daemon lives — runs
harbor as a broker job (`Dispatcher.job`), speaking for it: a started
`progress{}`, then a result carrying the artifact ref of the job's whole run.
The config and the bundle harbor resolves from the checkout its environment
came from (`var/benchmark/bundle`) are the workspace tree's, named absolutely
since the job runs outside the tree; the score is written into the run's own
output directory, and reaches the session as artifact content.

Both halves of the kind live here. `benchmark_kind` is the host-side handler
factory the `bro.broker_kinds` entry point targets; its args are
`{config, timeout?}` — `config` a job-config path relative to the workspace
root (the same spelling inside a container session and on the host), `timeout`
the seconds before the host kills the run. Only the session root may start
jobs; everything else is denied. The `benchmark-job` CLI is the session-side
client: `start` sends the request (`--detach` prints the request id and exits —
the natural mode for a multi-hour run), and `check` reattaches by request id
over the broxy's retention — a non-marking peek by default, `--wait` to block
and collect, `--last-seen` for the cursor re-read of an already-collected
result. Either way what it prints is the run's ref, which `artifact get` turns
into a readable path.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import bro.base.args as base_args
from bro.base import log
from bro.broker.brotocol import Message, Tag
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.dispatcher import Dispatcher, RequestHandler
from bro.broker.job import OUTPUT_DIRECTORY, CommandJob
from bro.broker.runtime import Peer
from bro.kinds import KindContext, tree_path

__cli_name__ = 'benchmark-job'

BENCHMARK = 'benchmark'  # the kind a benchmark request names
# request-lifecycle bound when the request names no timeout — generous: a full
# Terminal-Bench 2.1 job across two agents runs for hours
DEFAULT_TIMEOUT = 12 * 3600.0
# `benchmark-job check` exit code while the result is not in yet (0 = answer
# relayed, 1 = failure, 2 = argparse usage error)
PENDING_EXIT_CODE = 3

_ARGS_KEYS = frozenset({'config', 'timeout'})


# --- the host side: the `bro.broker_kinds` factory --------------------------------


def _refusal(workspace_tree: Path, args: dict[str, Any]) -> Optional[str]:
  """why the request is refused, or None when the job may start. Strict on shape
  (an unknown key is a caller bug, not a default) and on the config path: it must
  stay a file inside the workspace tree (`bro.kinds.tree_path`)."""
  unknown = sorted(set(args) - _ARGS_KEYS)
  if len(unknown) > 0:
    return f'unknown benchmark field(s): {", ".join(unknown)}'
  config = args.get('config')
  if not isinstance(config, str) or len(config) == 0:
    return "benchmark needs a non-empty string 'config'"
  timeout = args.get('timeout')
  if timeout is not None and (
    not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0
  ):
    return "benchmark 'timeout' must be a positive number of seconds"
  try:
    resolved = tree_path(workspace_tree, config)
  except ValueError as e:
    return f'benchmark config: {e}'
  if not resolved.is_file():
    return f'no job config at {config!r} in the workspace'
  if not (workspace_tree / 'benchmark' / 'pyproject.toml').is_file():
    return 'the workspace carries no benchmark project'
  return None


def benchmark_kind(kind_context: KindContext) -> RequestHandler:
  """the `benchmark` kind for the session `kind_context` describes."""
  workspace_tree = kind_context.workspace_tree

  def handle(context: Dispatcher, peer: Peer, message: Message) -> None:
    args = message.args
    if context.root is None or peer != context.root:
      _deny(context, peer, 'benchmark denied: only the session root may start benchmark jobs')
      return
    error = _refusal(workspace_tree, args)
    if error is not None:
      _deny(context, peer, error)
      return
    config = args['config']
    timeout = args.get('timeout')
    command = (
      'uv',
      'run',
      '--project',
      str((workspace_tree / 'benchmark').resolve()),
      'harbor',
      'job',
      'start',
      '-c',
      str((workspace_tree / config).resolve()),
      '--jobs-dir',
      OUTPUT_DIRECTORY,
    )
    environment = dict(os.environ)
    environment['UV_PROJECT_ENVIRONMENT'] = str(
      (workspace_tree.parent / 'benchmark-venv').resolve()
    )
    context.job(
      CommandJob(command=command, env=environment),
      peer,
      timeout=float(timeout) if timeout is not None else DEFAULT_TIMEOUT,
    )
    log.info('benchmark: job started (request %s, config %s)', message.id, config)

  return handle


def _deny(context: Dispatcher, peer: Peer, error: str) -> None:
  log.warning('benchmark: %s', error)
  context.reply(peer, {'outcome': 'denied', 'error': error})


# --- the session side: the benchmark-job CLI ---------------------------------------


class JobError(Exception):
  """a benchmark job that produced no usable outcome: denied, failed, or its
  result never arrived. The message is the operator-facing reason; `ref` names
  the run a failing job still left behind, where there was one."""

  def __init__(self, reason: str, ref: Optional[str] = None):
    super().__init__(reason)
    self.ref = ref


def _open_client() -> Client:
  client = Client.from_env()
  if client is None:
    raise JobError(
      f'no broker channel ({CHANNEL_ENV} unset); benchmark jobs need a session channel'
    )
  return client


def _interpret_result(message: Message) -> str:
  """turn a benchmark result into the ref of the job's collected run, or raise
  `JobError` with the failure reason."""
  payload = message.payload
  outcome = payload.get('outcome')
  if outcome == 'ok':
    value = payload.get('value')
    ref = value.get('ref') if isinstance(value, dict) else None
    if ref is None:
      raise JobError(f'benchmark job answered ok with no run: {payload}')
    return str(ref)
  if outcome == 'denied':
    raise JobError(str(payload.get('error', payload)))
  detail = payload.get('detail')
  detail = detail if isinstance(detail, dict) else {}
  parts = [f'benchmark job failed ({detail.get("reason")})']
  if detail.get('exit_code') is not None:
    parts[0] += f' with exit code {detail["exit_code"]}'
  if detail.get('ref') is not None:
    parts.append(f'its run is artifact {detail["ref"]}')
  diagnostic = payload.get('error')
  if diagnostic is not None and len(str(diagnostic).strip()) > 0:
    parts.append(str(diagnostic).strip())
  raise JobError('; '.join(parts), ref=detail.get('ref'))


def _await_outcome(client: Client, request: Message, timeout: float) -> str:
  """block for the request's result and interpret it. The host's started
  progress re-arms the deadline, so `timeout` bounds the silence since the last
  message rather than the whole wait."""
  try:
    result = client.await_reply(
      request,
      timeout,
      on_interim=lambda message: log.info('benchmark job launched'),
      timeout_after_interim=timeout,
    )
  except TimeoutError:
    raise JobError(
      f'no result within {timeout:.0f}s — the job may still be running; '
      f'reattach with `benchmark-job check {request.exchange}`'
    ) from None
  except ConnectionError as e:
    raise JobError(f'broker channel closed awaiting the job result: {e}') from None
  return _interpret_result(result)


def _relay(await_outcome: Callable[[], str]) -> int:
  try:
    result = await_outcome()
  except JobError as e:
    log.error('%s', e)
    return 1
  print(result)
  return 0


def _job_args(config: str, timeout: Optional[float]) -> dict[str, Any]:
  args: dict[str, Any] = {'config': config}
  if timeout is not None:
    args['timeout'] = timeout
  return args


def run_job(config: str, timeout: Optional[float] = None) -> str:
  """start a benchmark job and block until the host answers, returning the ref
  of its collected run. Raises `JobError` with the operator-facing reason."""
  with _open_client() as client:
    request = client.send(BENCHMARK, _job_args(config, timeout))
    log.info('benchmark job request %s', request.exchange)
    return _await_outcome(client, request, timeout if timeout is not None else DEFAULT_TIMEOUT)


def _start(config: str, timeout: Optional[float], detach: bool) -> int:
  if not detach:
    return _relay(lambda: run_job(config, timeout))
  try:
    client = _open_client()
  except JobError as e:
    log.error('%s', e)
    return 1
  with client:
    request = client.send(BENCHMARK, _job_args(config, timeout))
    log.info('benchmark job request %s', request.exchange)
    print(request.exchange)
    return 0


def _collect(request_id: str, timeout: Optional[float]) -> str:
  from bro.broker.broxy import CLAIM_KIND

  with _open_client() as client:
    claim = client.send(CLAIM_KIND, {'id': request_id})
    return _await_outcome(client, claim, timeout if timeout is not None else DEFAULT_TIMEOUT)


def _check(request_id: str, wait: bool, timeout: Optional[float], last_seen: Optional[int]) -> int:
  from bro.broker.broxy import CheckDenied, check

  if wait and last_seen is not None:
    log.error('--last-seen is a cursor read; it does not combine with --wait')
    return 1
  if timeout is not None and not wait:
    log.error('--timeout only bounds a --wait; a plain check never blocks')
    return 1
  if wait:
    return _relay(lambda: _collect(request_id, timeout))
  try:
    with _open_client() as client:
      report = check(client, request_id, last_seen=last_seen)
  except (JobError, CheckDenied, TimeoutError, ConnectionError) as e:
    log.error('%s', e)
    return 1
  result = next((m for m in report.conversation if m.type == Tag.RESULT), None)
  if result is None:
    if report.state == 'pending':
      log.info('benchmark job still running')
      return PENDING_EXIT_CODE
    through = f' (read through seq {report.seq})' if report.seq is not None else ''
    log.info(
      'benchmark job result was already read%s; re-read the conversation with '
      '`benchmark-job check %s --last-seen 0`',
      through,
      request_id,
    )
    return 1
  return _relay(lambda: _interpret_result(result))


def main(argv: list[str]) -> Optional[int]:
  if len(argv) > 1 and argv[1] == 'check':
    parser = base_args.Parser(
      prog='benchmark-job check',
      description="check on a benchmark job by its request id: print its run's "
      f'artifact ref if the result is in, otherwise report `still running` and exit '
      f'{PENDING_EXIT_CODE} without blocking; --wait blocks and collects instead',
    )
    parser.add_argument('request_id', help='request id printed by benchmark-job start')
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
      metavar='SECONDS',
      help='with --wait: seconds the job was given (bounds the wait; default: the job default)',
    )
    parser.add_argument(
      '--last-seen',
      type=int,
      help='cursor read: replay the conversation from this sequence (0 = the '
      'start) regardless of read status — recovers a result that was already '
      'read by a dead wait; not combinable with --wait',
    )
    return _check(**parser.parse(argv[1:]))
  if len(argv) > 1 and argv[1] == 'start':
    parser = base_args.Parser(
      prog='benchmark-job start',
      description='start a benchmark job through the session broker: the host runs '
      'harbor in this workspace with its own docker access and prints the artifact '
      'ref of the finished run, whose output/ holds the score; use `benchmark-job '
      'check` to reattach to a request',
    )
    parser.add_argument(
      '-c', '--config', required=True, help='job config path, relative to the workspace root'
    )
    parser.add_argument(
      '--timeout',
      type=float,
      metavar='SECONDS',
      help=f'seconds before the host kills the job (default: {DEFAULT_TIMEOUT:.0f})',
    )
    parser.add_argument(
      '--detach',
      action='store_true',
      help='print the request id and exit after sending; collect it with benchmark-job check',
    )
    return _start(**parser.parse(argv[1:]))
  log.error('usage: benchmark-job start|check …; each verb takes --help')
  return 2
