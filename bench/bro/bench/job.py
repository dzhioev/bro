#!/usr/bin/env python
"""Start and inspect raw Harbor jobs through a managed session's broker.

The `benchmark` worker type accepts a workspace-relative config and an optional timeout through `launch`.
The host runs `bro.benchmark.job` with Docker access and collects its whole command-job directory as the result artifact.

The `benchmark-job` session command starts that work or reads its retained journal result.
Detached starts print the accepted request id, and completed work prints the artifact ref.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional, override

from bro.base import log
from bro.broker.environment import BROKER_CHANNEL
from bro.worker_types import Job, LaunchDenied, LaunchRequest, WorkerType, tree_path

if TYPE_CHECKING:
  from bro.broker.brotocol import Message, Talk
  from bro.broker.client import Client

__cli_name__ = 'benchmark-job'

BENCHMARK = 'benchmark'
# request-lifecycle bound when the request names no timeout — generous: a full
# Terminal-Bench 2.1 job across two agents runs for hours
DEFAULT_TIMEOUT = 12 * 3600.0
# `benchmark-job check` exit code while the result is not in yet (0 = answer
# relayed, 1 = failure, 2 = argparse usage error)
PENDING_EXIT_CODE = 3
HARBOR_API_KEY_ENV = 'HARBOR_API_KEY'


# --- the registered worker type ---------------------------------------------------


class BenchmarkType(WorkerType):
  name = BENCHMARK
  default_timeout = DEFAULT_TIMEOUT

  @override
  def talk(self, request: LaunchRequest) -> Talk:
    return frozenset()

  @override
  def launch(self, request: LaunchRequest) -> Job:
    from bro.broker.job import OUTPUT_DIRECTORY, CommandJob

    unknown = sorted(set(request.args) - {'config'})
    if len(unknown) > 0:
      raise LaunchDenied(f'unknown benchmark field(s): {", ".join(unknown)}')
    config = request.args.get('config')
    if not isinstance(config, str) or len(config) == 0:
      raise LaunchDenied("benchmark needs a non-empty string 'config'")
    try:
      resolved = tree_path(request.owner.tree, config)
    except ValueError as error:
      raise LaunchDenied(f'benchmark config: {error}') from error
    if not resolved.is_file():
      raise LaunchDenied(f'no job config at {config!r} in the workspace')
    project = request.owner.tree / 'benchmark'
    if not (project / 'pyproject.toml').is_file():
      raise LaunchDenied('the workspace carries no benchmark project')

    environment = dict(os.environ)
    environment.pop(HARBOR_API_KEY_ENV, None)
    environment['UV_PROJECT_ENVIRONMENT'] = str(
      (request.owner.tree.parent / 'benchmark-venv').resolve()
    )
    # The job's interpreter is that environment's; a launcher venv left named
    # beside it is a second answer uv reports the conflict over.
    environment.pop('VIRTUAL_ENV', None)
    command = CommandJob(
      command=(
        'uv',
        'run',
        '--project',
        str(project.resolve()),
        'bro.benchmark.job',
        '-c',
        str(resolved),
        '--jobs-dir',
        OUTPUT_DIRECTORY,
      ),
      env=environment,
    )
    log.info('benchmark: job accepted (request %s, config %s)', request.id, config)
    return Job(command)


# --- the session side: the benchmark-job CLI ---------------------------------------


class JobError(Exception):
  """a benchmark job that produced no usable outcome: denied, failed, or its
  result never arrived. The message is the operator-facing reason; `ref` names
  the run a failing job still left behind, where there was one."""

  def __init__(self, reason: str, ref: Optional[str] = None):
    super().__init__(reason)
    self.ref = ref


def _open_client() -> Client:
  from bro.broker.client import Client

  client = Client.from_env()
  if client is None:
    raise JobError(
      f'no broker channel ({BROKER_CHANNEL} unset); benchmark jobs need a session channel'
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
  """Block for the request's result and interpret it.
  The host's `started` mark re-arms the deadline, so `timeout` bounds the silence since the last message rather than the whole wait."""
  from bro.broker.brotocol import Tag

  try:
    result = client.await_reply(
      request,
      timeout,
      on_interim=lambda message: (
        log.info('benchmark job launched')
        if message.type == Tag.MARK and message.payload.get('transition') == 'started'
        else None
      ),
      timeout_after_interim=timeout,
    )
  except TimeoutError:
    raise JobError(
      f'no result within {timeout:.0f}s — the job may still be running; '
      f'reattach with `benchmark-job check {request.request_id}`'
    ) from None
  except ConnectionError as e:
    raise JobError(f'broker channel closed awaiting the job result: {e}') from None
  return _interpret_result(result)


def _print_outcome(ref: str) -> None:
  print(ref)


def _relay(await_outcome: Callable[[], str]) -> int:
  try:
    ref = await_outcome()
    _print_outcome(ref)
  except JobError as e:
    log.error('%s', e)
    return 1
  return 0


def _job_args(config: str, timeout: Optional[float]) -> dict[str, Any]:
  args: dict[str, Any] = {'type': BENCHMARK, 'config': config}
  if timeout is not None:
    args['timeout'] = timeout
  return args


def run_job(config: str, timeout: Optional[float] = None) -> str:
  """start a benchmark job and block until the host answers, returning the ref
  of its collected run. Raises `JobError` with the operator-facing reason."""
  from bro.quest import LAUNCH

  with _open_client() as client:
    request = client.send(LAUNCH, _job_args(config, timeout))
    log.info('benchmark job request %s', request.request_id)
    return _await_outcome(client, request, timeout if timeout is not None else DEFAULT_TIMEOUT)


def _start(config: str, timeout: Optional[float], detach: bool) -> int:
  from bro.broker.brotocol import Tag
  from bro.quest import ACCEPT_TIMEOUT, LAUNCH

  if not detach:
    return _relay(lambda: run_job(config, timeout))
  try:
    client = _open_client()
  except JobError as e:
    log.error('%s', e)
    return 1
  with client:
    request = client.send(LAUNCH, _job_args(config, timeout))
    log.info('benchmark job request %s', request.request_id)
    try:
      first = client.await_any(request, ACCEPT_TIMEOUT)
    except (TimeoutError, ConnectionError) as error:
      log.error('benchmark job acceptance failed: %s', error)
      return 1
    if first.type == Tag.RESULT:
      return _relay(lambda: _interpret_result(first))
    if first.type != Tag.MARK or first.payload.get('transition') != 'accepted':
      log.error('unexpected first benchmark job reply: %s', first.payload)
      return 1
    print(request.request_id)
    return 0


def _query_job(client: Client, request_id: str, *, wait_seconds: float = 0) -> dict[str, Any]:
  from bro.quest import ACCEPT_TIMEOUT, LAUNCH

  args: dict[str, Any] = {'id': request_id}
  if wait_seconds > 0:
    args['wait'] = wait_seconds
  try:
    result = client.call(
      'query',
      args,
      max(ACCEPT_TIMEOUT, wait_seconds + ACCEPT_TIMEOUT),
    )
  except (TimeoutError, ConnectionError) as error:
    raise JobError(f'benchmark journal query failed: {error}') from None
  payload = result.payload
  if payload.get('outcome') != 'ok':
    raise JobError(str(payload.get('error', payload)))
  value = payload.get('value')
  mission = value.get('mission') if isinstance(value, dict) else None
  if not isinstance(mission, dict):
    raise JobError(f'query for {request_id!r} returned no quest record')
  if mission.get('state') != 'evicted' and (
    mission.get('kind') != LAUNCH or mission.get('type') != BENCHMARK
  ):
    raise JobError(f'quest {request_id!r} is not a benchmark job')
  return mission


def _queried_ref(quest: dict[str, Any]) -> Optional[str]:
  from bro.broker.brotocol import Message, Tag

  state = quest.get('state')
  if state in ('accepted', 'started'):
    return None
  request_id = quest.get('id')
  if state == 'evicted' or quest.get('result_evicted') is True:
    raise JobError(f'benchmark job {request_id!r} result is no longer retained')
  if state not in ('ended', 'denied'):
    raise JobError(f'benchmark job {request_id!r} has unknown state {state!r}')
  payload = quest.get('result')
  if not isinstance(payload, dict):
    raise JobError(f'benchmark job {request_id!r} has no retained result')
  return _interpret_result(Message(type=Tag.RESULT, request=str(request_id), payload=payload))


def _wait_for_job(request_id: str, timeout: Optional[float]) -> str:
  from bro.quest import READ_WAIT_SECONDS

  if timeout is not None and timeout <= 0:
    raise JobError('benchmark query wait must be positive')
  wait_seconds = min(
    timeout if timeout is not None else READ_WAIT_SECONDS,
    READ_WAIT_SECONDS,
  )
  with _open_client() as client:
    while True:
      ref = _queried_ref(_query_job(client, request_id, wait_seconds=wait_seconds))
      if ref is not None:
        return ref


def _check(request_id: str, wait: bool, timeout: Optional[float]) -> int:
  if timeout is not None and not wait:
    log.error('--timeout only sets the long-poll interval for --wait')
    return 1
  if wait:
    return _relay(lambda: _wait_for_job(request_id, timeout))
  try:
    with _open_client() as client:
      ref = _queried_ref(_query_job(client, request_id))
  except JobError as error:
    log.error('%s', error)
    return 1
  if ref is None:
    log.info('benchmark job still running')
    return PENDING_EXIT_CODE
  return _relay(lambda: ref)


def main(argv: list[str]) -> Optional[int]:
  import bro.base.args as base_args
  from bro.quest import READ_WAIT_SECONDS

  if len(argv) > 1 and argv[1] == 'check':
    parser = base_args.Parser(
      prog='benchmark-job check',
      description="check on a benchmark job by its request id: print its run's "
      f'artifact ref if the result is in, otherwise report `still running` and exit '
      f'{PENDING_EXIT_CODE} without blocking; --wait long-polls the same journal record',
    )
    parser.add_argument('request_id', help='request id printed by benchmark-job start')
    parser.add_argument(
      '--wait',
      action='store_true',
      help='block until the retained terminal result arrives; concurrent and later '
      'reads are safe because journal queries are non-destructive',
    )
    parser.add_argument(
      '--timeout',
      type=float,
      metavar='SECONDS',
      help=f'with --wait: maximum seconds per journal long-poll (default: {READ_WAIT_SECONDS:.0f})',
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
      help='print the accepted quest id and exit; read its result with benchmark-job check',
    )
    return _start(**parser.parse(argv[1:]))
  log.error('usage: benchmark-job start|check …; each verb takes --help')
  return 2
