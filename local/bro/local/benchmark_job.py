#!/usr/bin/env python
"""benchmark-job — run this checkout's benchmark jobs through the session broker.

The `benchmark` kind lets a managed session start a benchmark run without
holding any docker authority of its own: the session sends one coarse request
over its broker channel, and the host — where the docker daemon lives — runs
harbor as a broker job (`Dispatcher.job`), speaking for it through accepted and
started marks, then a result carrying the artifact ref of the job's whole run.
The config and the bundle harbor resolves from the checkout its environment
came from (`var/benchmark/bundle`) are the workspace tree's, named absolutely
since the job runs outside the tree; the score is written into the run's own
output directory, and reaches the session as artifact content.

Both halves of the kind live here. `benchmark_kind` is the host-side handler
factory the `bro.broker_kinds` entry point targets; its args are
`{config, timeout?, upload}` — `config` a job-config path relative to the workspace
root (the same spelling inside a container session and on the host), `timeout`
the seconds before the host kills the run, and `upload` its Harbor Hub visibility
or `none`. Only the session root may start
jobs; everything else is denied. The `benchmark-job` CLI is the session-side
client: `start` sends the request (`--detach` prints the quest id after host
acceptance — the natural mode for a multi-hour run), and `check` reads the
journal by quest id, once by default or in a repeatable long-poll loop with
`--wait`. Either way it prints the run's ref and, for an uploaded run, its Harbor
Hub link; `artifact get` turns the ref into a readable path.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import bro.base.args as base_args
from bro.artifact import ArtifactError, get_artifact
from bro.base import credentials, log
from bro.broker.brotocol import Message, Tag
from bro.broker.client import CHANNEL_ENV, Client
from bro.broker.dispatcher import Dispatcher, RequestHandler
from bro.broker.job import OUTPUT_DIRECTORY, CommandJob
from bro.broker.runtime import Peer
from bro.kinds import KindContext, tree_path
from bro.summon import ACCEPT_TIMEOUT, READ_WAIT_SECONDS

__cli_name__ = 'benchmark-job'

BENCHMARK = 'benchmark'  # the kind a benchmark request names
# request-lifecycle bound when the request names no timeout — generous: a full
# Terminal-Bench 2.1 job across two agents runs for hours
DEFAULT_TIMEOUT = 12 * 3600.0
# `benchmark-job check` exit code while the result is not in yet (0 = answer
# relayed, 1 = failure, 2 = argparse usage error)
PENDING_EXIT_CODE = 3
UPLOAD_VISIBILITIES = ('none', 'private', 'public')
UPLOAD_RECORD = 'upload.json'
HARBOR_CREDENTIAL = 'harbor'
HARBOR_API_KEY_ENV = 'HARBOR_API_KEY'

_ARGS_KEYS = frozenset({'config', 'timeout', 'upload'})


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
  upload = args.get('upload')
  if upload not in UPLOAD_VISIBILITIES:
    return f"benchmark 'upload' must be one of: {', '.join(UPLOAD_VISIBILITIES)}"
  try:
    resolved = tree_path(workspace_tree, config)
  except ValueError as e:
    return f'benchmark config: {e}'
  if not resolved.is_file():
    return f'no job config at {config!r} in the workspace'
  if not (workspace_tree / 'benchmark' / 'pyproject.toml').is_file():
    return 'the workspace carries no benchmark project'
  return None


def _harbor_api_key(credential_scope: frozenset[str]) -> str:
  names = sorted(
    name for name in credential_scope if credentials.parse_name(name)[0] == HARBOR_CREDENTIAL
  )
  if len(names) == 0:
    raise credentials.SecretNotFound(HARBOR_CREDENTIAL)
  if len(names) != 1:
    raise ValueError(f'benchmark scope holds multiple Harbor credentials: {", ".join(names)}')
  return credentials.default_store().get_instance(names[0])


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
      'bro.benchmark.job',
      '-c',
      str((workspace_tree / config).resolve()),
      '--jobs-dir',
      OUTPUT_DIRECTORY,
      '--upload',
      args['upload'],
    )
    environment = dict(os.environ)
    environment.pop(HARBOR_API_KEY_ENV, None)
    if args['upload'] != 'none':
      try:
        environment[HARBOR_API_KEY_ENV] = _harbor_api_key(kind_context.credential_scope)
      except (credentials.SecretNotFound, ValueError) as error:
        _deny(context, peer, f'benchmark upload credential: {error}')
        return
    environment['UV_PROJECT_ENVIRONMENT'] = str(
      (workspace_tree.parent / 'benchmark-venv').resolve()
    )
    # the job's interpreter is that environment's; a launcher venv left named
    # beside it is a second answer uv reports the conflict over
    environment.pop('VIRTUAL_ENV', None)
    context.job(
      CommandJob(command=command, env=environment),
      peer,
      timeout=float(timeout) if timeout is not None else DEFAULT_TIMEOUT,
    )
    log.info('benchmark: job started (request %s, config %s)', message.id, config)

  return handle


def _deny(context: Dispatcher, peer: Peer, error: str) -> None:
  log.warning('benchmark: %s', error)
  context.deny(peer, error)


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
      f'reattach with `benchmark-job check {request.quest_id}`'
    ) from None
  except ConnectionError as e:
    raise JobError(f'broker channel closed awaiting the job result: {e}') from None
  return _interpret_result(result)


def uploaded_job_url(run_directory: Path) -> Optional[str]:
  records = sorted((run_directory / OUTPUT_DIRECTORY).glob(f'*/{UPLOAD_RECORD}'))
  if len(records) == 0:
    return None
  if len(records) != 1:
    raise JobError(f'{run_directory / OUTPUT_DIRECTORY} holds multiple upload records')
  try:
    record = json.loads(records[0].read_text())
  except json.JSONDecodeError as error:
    raise JobError(f'malformed upload record at {records[0]}: {error}') from error
  if (
    not isinstance(record, dict)
    or set(record) != {'visibility', 'url'}
    or record.get('visibility') not in {'private', 'public'}
    or not isinstance(record.get('url'), str)
    or len(record['url']) == 0
  ):
    raise JobError(f'malformed upload record at {records[0]}')
  return record['url']


def _print_outcome(ref: str) -> None:
  print(ref)
  try:
    run_directory = Path(get_artifact(ref))
  except ArtifactError as error:
    log.warning('the run is artifact %s, which did not resolve: %s', ref, error)
    return
  url = uploaded_job_url(run_directory)
  if url is not None:
    print(f'upload  {url}')


def _relay(await_outcome: Callable[[], str]) -> int:
  try:
    ref = await_outcome()
    _print_outcome(ref)
  except JobError as e:
    log.error('%s', e)
    return 1
  return 0


def _job_args(config: str, timeout: Optional[float], upload: str) -> dict[str, Any]:
  args: dict[str, Any] = {'config': config, 'upload': upload}
  if timeout is not None:
    args['timeout'] = timeout
  return args


def run_job(config: str, timeout: Optional[float] = None, upload: str = 'none') -> str:
  """start a benchmark job and block until the host answers, returning the ref
  of its collected run. Raises `JobError` with the operator-facing reason."""
  with _open_client() as client:
    request = client.send(BENCHMARK, _job_args(config, timeout, upload))
    log.info('benchmark job request %s', request.quest_id)
    return _await_outcome(client, request, timeout if timeout is not None else DEFAULT_TIMEOUT)


def _start(config: str, timeout: Optional[float], upload: str, detach: bool) -> int:
  if not detach:
    return _relay(lambda: run_job(config, timeout, upload))
  try:
    client = _open_client()
  except JobError as e:
    log.error('%s', e)
    return 1
  with client:
    request = client.send(BENCHMARK, _job_args(config, timeout, upload))
    log.info('benchmark job request %s', request.quest_id)
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
    print(request.quest_id)
    return 0


def _query_job(client: Client, request_id: str, *, wait_seconds: float = 0) -> dict[str, Any]:
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
  quest = value.get('quest') if isinstance(value, dict) else None
  if not isinstance(quest, dict):
    raise JobError(f'query for {request_id!r} returned no quest record')
  if quest.get('kind') != BENCHMARK:
    raise JobError(f'quest {request_id!r} is not a benchmark job')
  return quest


def _queried_ref(quest: dict[str, Any]) -> Optional[str]:
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
  return _interpret_result(Message(type=Tag.RESULT, quest=str(request_id), payload=payload))


def _wait_for_job(request_id: str, timeout: Optional[float]) -> str:
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
      '--upload',
      choices=UPLOAD_VISIBILITIES,
      default='none',
      help='Harbor Hub visibility, or none to skip upload (default: none)',
    )
    parser.add_argument(
      '--detach',
      action='store_true',
      help='print the accepted quest id and exit; read its result with benchmark-job check',
    )
    return _start(**parser.parse(argv[1:]))
  log.error('usage: benchmark-job start|check …; each verb takes --help')
  return 2
