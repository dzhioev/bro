import asyncio
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import pytest

from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.dispatcher import (
  DEFAULT_TIMEOUT,
  PING,
  Dispatcher,
  ping_handler,
  spawn_test_handler,
)
from bro.broker.job import STATUS_FILE, STDOUT_FILE, CommandJob
from bro.broker.runtime import Peer, job_peer
from bro.broker.spawn import LaunchSpec
from bro.broker.transport import Provisioned

_LAUNCH = LaunchSpec()  # opaque marker; the fake Runtime never inspects it
_JOB = CommandJob(command=('true',), env={})  # ditto
_REF = 'sha256:' + '0' * 64


def _request(kind: str, request_id: str, args: Optional[dict] = None) -> Message:
  return Message(
    type=Tag.REQUEST, id=request_id, payload={'kind': kind, 'args': args if args is not None else {}}
  )  # fmt: skip


class FakeRuntime:
  """records the commands the Dispatcher issues and hands it test-chosen peer ids.

  Structurally a `bro.broker.dispatcher.RuntimeCommands`; the Dispatcher drives it while the
  test drives the Dispatcher's listener callbacks, so the rules are exercised with no loop,
  socket, or subprocess.
  """

  def __init__(self):
    self.sent: list[tuple[Peer, Message]] = []
    self.spawns: list[tuple[LaunchSpec, Optional[float], str]] = []
    self.jobs: list[tuple[CommandJob, Optional[float], str]] = []
    self.job_directories: list[Path] = []
    self.expects: list[Optional[float]] = []
    self.forgotten: list[Peer] = []
    self.killed: list[Peer] = []
    self.next_peers: list[Peer] = []  # spawn/expect returns these front-to-back
    self.spawn_error: Optional[BaseException] = None
    self._stopped = asyncio.Event()

  async def spawn(self, launch: LaunchSpec, *, timeout: Optional[float], exchange: str) -> Peer:
    self.spawns.append((launch, timeout, exchange))
    if self.spawn_error is not None:
      raise self.spawn_error
    return self.next_peers.pop(0)

  async def job(
    self, command: CommandJob, *, directory: Path, timeout: Optional[float], exchange: str
  ) -> Peer:
    self.jobs.append((command, timeout, exchange))
    self.job_directories.append(directory)
    if self.spawn_error is not None:
      raise self.spawn_error
    return job_peer(exchange)

  async def expect(self, *, timeout: Optional[float]) -> Provisioned:
    self.expects.append(timeout)
    if self.spawn_error is not None:
      raise self.spawn_error
    peer = self.next_peers.pop(0)
    return Provisioned(channel=peer, host_endpoint=f'/broker/{peer}.sock')

  def send(self, peer: Peer, message: Message) -> None:
    self.sent.append((peer, message))

  def kill(self, peer: Peer) -> None:
    self.killed.append(peer)

  def forget(self, peer: Peer) -> None:
    self.forgotten.append(peer)

  async def serve(self) -> None:
    await self._stopped.wait()

  async def stop(self) -> None:
    self._stopped.set()


class FakeJobOutput:
  """a `bro.broker.dispatcher.JobOutput` over real temp directories: records what
  it was handed and answers with a fixed value, or raises `collect_error`."""

  def __init__(self):
    self.opened: list[Path] = []
    self.collected: list[tuple[Path, Peer]] = []
    self.runs: list[dict[str, str]] = []  # each run's top-level files, as collect found them
    self.collect_error: Optional[BaseException] = None

  def open(self) -> Path:
    directory = Path(tempfile.mkdtemp())
    self.opened.append(directory)
    return directory

  async def collect(self, directory: Path, context: Dispatcher, requester: Peer) -> dict:
    self.collected.append((directory, requester))
    self.runs.append(
      {entry.name: entry.read_text() for entry in sorted(directory.iterdir()) if entry.is_file()}
    )
    if self.collect_error is not None:
      raise self.collect_error
    return {'ref': _REF}

  def status(self, index: int = 0) -> dict[str, Any]:
    return json.loads(self.runs[index][STATUS_FILE])


def make_dispatcher() -> tuple[Dispatcher, FakeRuntime]:
  runtime = FakeRuntime()
  dispatcher = Dispatcher(job_output=FakeJobOutput())
  dispatcher.bind(runtime)
  return dispatcher, runtime


def job_output(dispatcher: Dispatcher) -> FakeJobOutput:
  output = dispatcher.job_output
  assert isinstance(output, FakeJobOutput)
  return output


async def _settle() -> None:
  # let a scheduled Runtime.spawn task and its worker-binding done-callback run.
  for _ in range(4):
    await asyncio.sleep(0)


async def _until(condition: Callable[[], bool]) -> None:
  """wait for a job's collection, whose steps run off the loop, to land."""
  deadline = time.monotonic() + 5.0
  while not condition():
    assert time.monotonic() < deadline, 'the awaited condition never held'
    await asyncio.sleep(0.005)


async def spawn_child(
  dispatcher: Dispatcher,
  runtime: FakeRuntime,
  *,
  requester: Peer = 'requester',
  request_id: str = 'R',
) -> Peer:
  """drive a spawn request through rule 2 + the spawn-test handler; return the worker peer."""
  child = 'child'
  runtime.next_peers.append(child)
  dispatcher.on('spawn-test', spawn_test_handler(_LAUNCH))
  dispatcher.on_message(requester, _request('spawn-test', request_id))
  await _settle()
  return child


@pytest.mark.asyncio
async def test_ping_replies_with_its_arguments_echoed():
  # rule 2: a request invokes its kind's handler, which replies with a result.
  dispatcher, runtime = make_dispatcher()
  dispatcher.on(PING, ping_handler)
  dispatcher.on_message('caller', _request(PING, 'Q', {'n': 7}))
  assert len(runtime.sent) == 1
  target, reply = runtime.sent[0]
  assert target == 'caller'
  assert reply.type == Tag.RESULT
  assert reply.request == 'Q'
  assert reply.payload == {'outcome': 'ok', 'value': {'n': 7}}


@pytest.mark.asyncio
async def test_unknown_kind_is_denied():
  dispatcher, runtime = make_dispatcher()
  dispatcher.on_message('caller', _request('mystery', 'Q'))
  [(target, denial)] = runtime.sent
  assert target == 'caller'
  assert denial.type == Tag.RESULT
  assert denial.request == 'Q'
  assert denial.payload['outcome'] == 'denied'
  assert 'mystery' in denial.payload['error']


@pytest.mark.asyncio
async def test_request_id_collision_is_denied():
  # ids are unique by entropy; one naming a live exchange is rejected, not coped with.
  dispatcher, runtime = make_dispatcher()
  await spawn_child(dispatcher, runtime)  # exchange R is live
  dispatcher.on_message('other', _request('spawn-test', 'R'))
  target, denial = runtime.sent[-1]
  assert target == 'other'
  assert (denial.type, denial.request, denial.payload['outcome']) == (Tag.RESULT, 'R', 'denied')


@pytest.mark.asyncio
async def test_spawn_opens_the_exchange_with_default_timeout_and_binds_the_worker():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  assert runtime.spawns == [(_LAUNCH, DEFAULT_TIMEOUT, 'R')]  # the exchange id rides the launch
  assert dispatcher.exchanges['R'].requester == 'requester'
  assert dispatcher.exchanges['R'].worker == child
  assert dispatcher.workers[child] == 'R'


@pytest.mark.asyncio
async def test_worker_messages_route_to_the_requester_unchanged():
  # rule 1: the worker's own progress/result, correlated to its exchange, forward as-is.
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  started = brotocol.progress('R', {'trail_id': 't'})
  done = brotocol.result('R', 'ok', value='answer')
  dispatcher.on_message(child, started)
  dispatcher.on_message(child, done)
  assert runtime.sent[-2] == ('requester', started)
  assert runtime.sent[-1] == ('requester', done)
  assert 'R' not in dispatcher.exchanges  # the result closed the exchange


@pytest.mark.asyncio
async def test_correlated_message_from_a_non_worker_peer_is_refused():
  # rule 1 requires the sender to *be* the worker: knowing an exchange id gains nothing.
  dispatcher, runtime = make_dispatcher()
  await spawn_child(dispatcher, runtime)
  delivered = len(runtime.sent)
  dispatcher.on_message('impostor', brotocol.result('R', 'ok', value='forged'))
  assert len(runtime.sent) == delivered  # dropped, not delivered
  assert 'R' in dispatcher.exchanges  # and the exchange stays open


@pytest.mark.asyncio
async def test_messages_naming_a_closed_exchange_are_dropped():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(child, brotocol.result('R', 'ok'))
  delivered = len(runtime.sent)
  dispatcher.on_message(child, brotocol.progress('R', {}))  # after the result
  assert len(runtime.sent) == delivered  # dropped, not routed


@pytest.mark.asyncio
async def test_result_then_exit_is_a_single_terminal():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(child, brotocol.result('R', 'ok'))
  dispatcher.on_exit(child, 0, '')  # exchange closed already -> no synthesized failed
  assert all(m.payload.get('outcome') != 'failed' for _, m in runtime.sent)
  assert runtime.forgotten == [child]
  assert child not in dispatcher.workers  # cleaned up


@pytest.mark.asyncio
async def test_exit_without_result_synthesizes_failed_exit():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_exit(child, 3, 'boom-traceback')
  target, failed = runtime.sent[-1]
  assert target == 'requester'
  assert failed.type == Tag.RESULT
  assert failed.request == 'R'
  assert failed.payload == {
    'outcome': 'failed',
    'detail': {'reason': 'exit', 'exit_code': 3, 'output_tail': 'boom-traceback'},
  }
  assert runtime.forgotten == [child]


@pytest.mark.asyncio
async def test_timeout_synthesizes_failed_and_the_later_exit_dedupes():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_timeout(child)
  target, failed = runtime.sent[-1]
  assert target == 'requester'
  assert (failed.type, failed.request) == (Tag.RESULT, 'R')
  assert failed.payload == {'outcome': 'failed', 'detail': {'reason': 'timeout'}}
  dispatcher.on_exit(child, -9, '')  # the Runtime-killed peer's reap, exchange closed already
  assert sum(1 for _, m in runtime.sent if m.payload.get('outcome') == 'failed') == 1
  assert runtime.forgotten == [child]


@pytest.mark.asyncio
async def test_spawn_failure_synthesizes_failed_launch_and_closes_the_exchange():
  # a launch that raises feeds back as result{failed, reason: 'launch'} instead of
  # leaving the requester to hang to its timeout.
  dispatcher, runtime = make_dispatcher()
  runtime.spawn_error = RuntimeError('image build exploded')
  dispatcher.on('spawn-test', spawn_test_handler(_LAUNCH))
  dispatcher.on_message('requester', _request('spawn-test', 'R'))
  await _settle()
  assert len(runtime.sent) == 1
  target, failed = runtime.sent[0]
  assert target == 'requester'
  assert (failed.type, failed.request) == (Tag.RESULT, 'R')
  assert failed.payload == {
    'outcome': 'failed',
    'error': 'image build exploded',
    'detail': {'reason': 'launch'},
  }
  assert dispatcher.exchanges == {}  # closed; nothing to hold for the never-launched worker
  assert dispatcher.workers == {}


async def start_job(
  dispatcher: Dispatcher,
  runtime: FakeRuntime,
  *,
  requester: Peer = 'requester',
  request_id: str = 'R',
) -> Peer:
  """drive a job request through rule 2 + an inline handler; return the job's
  synthetic worker peer."""
  dispatcher.on('job-test', lambda context, peer, message: context.job(_JOB, peer))
  dispatcher.on_message(requester, _request('job-test', request_id))
  await _settle()
  return job_peer(request_id)


@pytest.mark.asyncio
async def test_job_opens_the_exchange_with_the_worker_bound_up_front():
  # a job's peer id is derivable from the exchange id, so the worker binds before
  # the launch resolves — no exit can slip between launch and bind.
  dispatcher, runtime = make_dispatcher()
  dispatcher.on('job-test', lambda context, peer, message: context.job(_JOB, peer))
  dispatcher.on_message('requester', _request('job-test', 'R'))
  worker = job_peer('R')
  assert dispatcher.exchanges['R'].requester == 'requester'
  assert dispatcher.exchanges['R'].worker == worker  # bound before the launch resolved
  assert dispatcher.workers[worker] == 'R'
  await _settle()
  assert runtime.jobs == [(_JOB, DEFAULT_TIMEOUT, 'R')]
  assert runtime.job_directories == job_output(dispatcher).opened  # the run it will answer with


@pytest.mark.asyncio
async def test_a_broker_with_no_job_output_runs_no_jobs():
  runtime = FakeRuntime()
  dispatcher = Dispatcher()
  dispatcher.bind(runtime)
  dispatcher.on('job-test', lambda context, peer, message: context.job(_JOB, peer))
  with pytest.raises(RuntimeError):
    dispatcher.on_message('requester', _request('job-test', 'R'))


@pytest.mark.asyncio
async def test_job_launch_delivers_a_started_progress():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  worker = await start_job(dispatcher, runtime)
  [(target, started)] = runtime.sent
  assert target == 'requester'
  assert (started.type, started.request, started.payload) == (Tag.PROGRESS, 'R', {})
  assert [(source, target, m.type) for source, target, m in observed] == [
    (worker, 'requester', Tag.PROGRESS)
  ]


@pytest.mark.asyncio
async def test_job_clean_exit_closes_the_exchange_with_the_collected_run():
  dispatcher, runtime = make_dispatcher()
  output = job_output(dispatcher)
  worker = await start_job(dispatcher, runtime)
  dispatcher.on_exit(worker, 0, '')
  # the reap decides the outcome and forgets the exchange before the collection
  # it delivers from, so nothing else can answer it
  assert 'R' not in dispatcher.exchanges
  await _until(lambda: any(m.type == Tag.RESULT for _, m in runtime.sent))
  target, done = runtime.sent[-1]
  assert target == 'requester'
  assert (done.type, done.request) == (Tag.RESULT, 'R')
  assert done.payload == {'outcome': 'ok', 'value': {'ref': _REF}}
  assert output.collected == [(output.opened[0], 'requester')]
  assert output.status() == {'reason': 'exit', 'exit_code': 0}
  assert runtime.forgotten == [worker]
  await _until(lambda: not output.opened[0].exists())  # the store holds its own copy


@pytest.mark.asyncio
async def test_job_failing_exit_carries_the_run_into_failed_exit():
  dispatcher, runtime = make_dispatcher()
  worker = await start_job(dispatcher, runtime)
  dispatcher.on_exit(worker, 3, '')
  await _until(lambda: any(m.type == Tag.RESULT for _, m in runtime.sent))
  target, failed = runtime.sent[-1]
  assert target == 'requester'
  assert failed.payload == {
    'outcome': 'failed',
    'detail': {'reason': 'exit', 'exit_code': 3, 'ref': _REF},
  }


@pytest.mark.asyncio
async def test_job_timeout_answers_from_the_reap_with_one_result():
  # the Runtime's kill is still in flight when on_timeout fires, so the run is
  # not final: the reap carries the timeout reason into the single result.
  dispatcher, runtime = make_dispatcher()
  output = job_output(dispatcher)
  worker = await start_job(dispatcher, runtime)
  dispatcher.on_timeout(worker)
  assert [m for _, m in runtime.sent if m.type == Tag.RESULT] == []
  dispatcher.on_exit(worker, -15, '')
  await _until(lambda: any(m.type == Tag.RESULT for _, m in runtime.sent))
  terminals = [m for _, m in runtime.sent if m.type == Tag.RESULT]
  assert len(terminals) == 1
  assert terminals[0].payload == {
    'outcome': 'failed',
    'detail': {'reason': 'timeout', 'exit_code': -15, 'ref': _REF},
  }
  assert output.status() == {'reason': 'timeout', 'exit_code': -15}


@pytest.mark.asyncio
async def test_job_collection_failure_fails_a_clean_run_and_keeps_it():
  dispatcher, runtime = make_dispatcher()
  output = job_output(dispatcher)
  output.collect_error = RuntimeError('store is full')
  worker = await start_job(dispatcher, runtime)
  dispatcher.on_exit(worker, 0, '')
  await _until(lambda: any(m.type == Tag.RESULT for _, m in runtime.sent))
  _, failed = runtime.sent[-1]
  assert failed.payload == {
    'outcome': 'failed',
    'error': 'store is full',
    'detail': {'reason': 'output'},
  }
  assert output.opened[0].exists()  # the run is the only copy of what the job produced


@pytest.mark.asyncio
async def test_job_collection_failure_leaves_a_failing_exit_its_own_reason():
  dispatcher, runtime = make_dispatcher()
  job_output(dispatcher).collect_error = RuntimeError('store is full')
  worker = await start_job(dispatcher, runtime)
  dispatcher.on_exit(worker, 3, '')
  await _until(lambda: any(m.type == Tag.RESULT for _, m in runtime.sent))
  _, failed = runtime.sent[-1]
  assert failed.payload == {
    'outcome': 'failed',
    'error': 'store is full',
    'detail': {'reason': 'exit', 'exit_code': 3},
  }


@pytest.mark.asyncio
async def test_job_launch_failure_synthesizes_failed_launch_and_unbinds_the_worker():
  dispatcher, runtime = make_dispatcher()
  runtime.spawn_error = RuntimeError('no such command')
  dispatcher.on('job-test', lambda context, peer, message: context.job(_JOB, peer))
  dispatcher.on_message('requester', _request('job-test', 'R'))
  await _settle()
  [(target, failed)] = runtime.sent  # no started progress precedes the failure
  assert (target, failed.type, failed.request) == ('requester', Tag.RESULT, 'R')
  assert failed.payload == {
    'outcome': 'failed',
    'error': 'no such command',
    'detail': {'reason': 'launch'},
  }
  assert dispatcher.exchanges == {}
  assert dispatcher.workers == {}
  assert not job_output(dispatcher).opened[0].exists()  # no process ran, nothing to collect


@pytest.mark.asyncio
async def test_duplicate_kind_registration_is_refused():
  dispatcher, _ = make_dispatcher()
  dispatcher.on(PING, ping_handler)
  with pytest.raises(ValueError):
    dispatcher.on(PING, ping_handler)


@pytest.mark.asyncio
async def test_job_round_trips_over_a_real_runtime_and_transport():
  # the one integration pass over the real loop: a requester on a live channel
  # sends the request, the job's process really runs, and the host's started
  # progress precedes the derived result.
  import os
  import sys

  from bro.broker.client import Client
  from bro.broker.runtime import Runtime
  from bro.broker.spawn import Spawner
  from bro.broker.transport import connect
  from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport

  class NoSpawner(Spawner):
    async def spawn(self, launch, channel, exchange):
      raise AssertionError('no worker peers in this test')

  transport = TcpServerTransport([LOCAL_HOST])
  output = FakeJobOutput()
  dispatcher = Dispatcher(job_output=output)
  runtime = Runtime(transport, NoSpawner(), dispatcher)
  dispatcher.bind(runtime)
  serve_task = asyncio.create_task(runtime.serve())
  await asyncio.sleep(0)  # let serve install the sink before the requester connects
  job = CommandJob(
    # outlive the launch callback so the started progress deterministically
    # precedes the collected result
    command=(sys.executable, '-c', 'import time; print("job-ran"); time.sleep(0.2)'),
    env=dict(os.environ),
  )
  dispatcher.on('job-test', lambda context, peer, message: context.job(job, peer, timeout=30.0))
  provisioned = await runtime.expect(timeout=None)

  def _drive() -> tuple[list[Message], Message]:
    interim: list[Message] = []
    with Client(connect(provisioned.host_endpoint.address(LOCAL_HOST))) as client:
      result = client.call('job-test', {}, 10.0, on_interim=interim.append)
    return interim, result

  try:
    interim, result = await asyncio.to_thread(_drive)
  finally:
    await runtime.stop()
    serve_task.cancel()
    await asyncio.gather(serve_task, return_exceptions=True)
  [started] = interim
  assert (started.type, started.payload) == (Tag.PROGRESS, {})
  assert result.payload == {'outcome': 'ok', 'value': {'ref': _REF}}
  assert output.runs[0][STDOUT_FILE].strip() == 'job-ran'  # the run the ref stands for


async def expect_child(
  dispatcher: Dispatcher,
  runtime: FakeRuntime,
  *,
  requester: Peer = 'requester',
  request_id: str = 'R',
) -> tuple[Peer, list[Provisioned]]:
  """drive an expect request through rule 2 + an inline handler; return the external
  peer and the provisioned channels the ready callback received."""
  child = 'external'
  runtime.next_peers.append(child)
  provisioned: list[Provisioned] = []
  dispatcher.on(
    'expect',
    lambda context, peer, message: context.expect(peer, timeout=None, ready=provisioned.append),
  )
  dispatcher.on_message(requester, _request('expect', request_id))
  await _settle()
  return child, provisioned


@pytest.mark.asyncio
async def test_expect_opens_the_exchange_and_hands_the_channel_to_ready():
  dispatcher, runtime = make_dispatcher()
  child, provisioned = await expect_child(dispatcher, runtime)
  assert runtime.expects == [None]
  assert [p.channel for p in provisioned] == [child]
  assert dispatcher.exchanges['R'].requester == 'requester'
  assert dispatcher.exchanges['R'].worker == child
  assert dispatcher.workers[child] == 'R'


@pytest.mark.asyncio
async def test_expected_peer_messages_route_and_gone_after_the_result_is_clean():
  # an expected worker's progress/result route like a spawned one's; the trailing
  # on_gone finds the exchange closed and only cleans up.
  dispatcher, runtime = make_dispatcher()
  child, _ = await expect_child(dispatcher, runtime)
  dispatcher.on_message(child, brotocol.progress('R', {'trail_id': 't'}))
  dispatcher.on_message(child, brotocol.result('R', 'ok'))
  assert [(target, m.type, m.request) for target, m in runtime.sent] == [
    ('requester', Tag.PROGRESS, 'R'),
    ('requester', Tag.RESULT, 'R'),
  ]
  dispatcher.on_gone(child)
  assert sum(1 for _, m in runtime.sent if m.payload.get('outcome') == 'failed') == 0
  assert runtime.forgotten == [child]
  assert child not in dispatcher.workers


@pytest.mark.asyncio
async def test_gone_without_a_result_synthesizes_failed_disconnected():
  dispatcher, runtime = make_dispatcher()
  child, _ = await expect_child(dispatcher, runtime)
  dispatcher.on_gone(child)
  target, failed = runtime.sent[-1]
  assert (target, failed.type, failed.request) == ('requester', Tag.RESULT, 'R')
  assert failed.payload == {'outcome': 'failed', 'detail': {'reason': 'disconnected'}}
  assert runtime.forgotten == [child]


@pytest.mark.asyncio
async def test_expect_failure_synthesizes_failed_launch():
  dispatcher, runtime = make_dispatcher()
  runtime.spawn_error = RuntimeError('no socket dir')
  provisioned: list[Provisioned] = []
  dispatcher.on(
    'expect',
    lambda context, peer, message: context.expect(peer, timeout=None, ready=provisioned.append),
  )
  dispatcher.on_message('requester', _request('expect', 'R'))
  await _settle()
  assert provisioned == []
  [(target, failed)] = runtime.sent
  assert (target, failed.type, failed.request) == ('requester', Tag.RESULT, 'R')
  assert failed.payload == {
    'outcome': 'failed',
    'error': 'no socket dir',
    'detail': {'reason': 'launch'},
  }
  assert dispatcher.exchanges == {}


@pytest.mark.asyncio
async def test_uncorrelatable_answers_are_refused():
  # rule 3: progress/result naming no live exchange -> dropped, nothing delivered.
  dispatcher, runtime = make_dispatcher()
  dispatcher.on_message('stranger', brotocol.progress('nobody', {}))
  dispatcher.on_message('stranger', brotocol.result('nobody', 'ok'))
  assert runtime.sent == []


@pytest.mark.asyncio
async def test_root_answers_its_host_anchored_exchange_without_peer_delivery(caplog):
  # run() opens the session's own exchange for the root; its progress/result reach
  # only the observers (target None), close nothing peer-visible, and never arm
  # drop-gating against the channel's later traffic.
  dispatcher, runtime = make_dispatcher()
  observed: list[tuple[Optional[Peer], Optional[Peer], Message]] = []
  dispatcher.add_delivery_observer(
    lambda source, target, message: observed.append((source, target, message))
  )
  runtime.next_peers.append('root')
  run_task = asyncio.ensure_future(dispatcher.run(_LAUNCH))
  await _settle()
  [(_, _, exchange)] = runtime.spawns
  dispatcher.on_message('root', brotocol.progress(exchange, {'trail_id': 't'}))
  dispatcher.on_message('root', brotocol.result(exchange, 'ok', value='done'))
  assert runtime.sent == []  # nobody to deliver to: the host is the requester
  assert [(source, target, m.type) for source, target, m in observed] == [
    ('root', None, Tag.PROGRESS),
    ('root', None, Tag.RESULT),
  ]
  assert not any('refused' in record.message for record in caplog.records)
  dispatcher.on(PING, ping_handler)
  dispatcher.on_message('root', _request(PING, 'Q'))
  assert runtime.sent[-1][1].payload['outcome'] == 'ok'  # the channel keeps serving
  dispatcher.stop()
  await asyncio.wait_for(run_task, 5)


def make_tap(dispatcher: Dispatcher) -> list[tuple[Optional[Peer], Optional[Peer], Message]]:
  observed: list[tuple[Optional[Peer], Optional[Peer], Message]] = []
  dispatcher.add_delivery_observer(
    lambda source, target, message: observed.append((source, target, message))
  )
  return observed


@pytest.mark.asyncio
async def test_delivery_tap_observes_rule_1_forwarding():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(child, brotocol.progress('R', {'trail_id': 't'}))
  dispatcher.on_message(child, brotocol.result('R', 'ok', value='answer'))
  assert [(source, target, message.type) for source, target, message in observed] == [
    (child, 'requester', Tag.PROGRESS),
    (child, 'requester', Tag.RESULT),
  ]
  assert observed[0][2].request == 'R'


@pytest.mark.asyncio
async def test_delivery_tap_observes_synthesized_failed():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_exit(child, 3, 'tail')
  [(source, target, failed)] = observed
  assert (source, target, failed.type) == (child, 'requester', Tag.RESULT)
  assert failed.payload['detail']['reason'] == 'exit'


@pytest.mark.asyncio
async def test_delivery_tap_observes_launch_failure_with_no_source_peer():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  runtime.spawn_error = RuntimeError('boom')
  dispatcher.on('spawn-test', spawn_test_handler(_LAUNCH))
  dispatcher.on_message('requester', _request('spawn-test', 'R'))
  await _settle()
  [(source, target, failed)] = observed
  assert source is None  # the worker never existed
  assert (target, failed.type) == ('requester', Tag.RESULT)
  assert failed.payload['detail']['reason'] == 'launch'


@pytest.mark.asyncio
async def test_delivery_tap_ignores_handler_replies_denials_and_refusals():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  dispatcher.on(PING, ping_handler)
  dispatcher.on_message('caller', _request(PING, 'Q'))  # rule 2 + reply()
  dispatcher.on_message('caller', _request('mystery', 'M'))  # denied
  dispatcher.on_message('stranger', brotocol.progress('nobody', {}))  # rule 3
  assert observed == []
  assert len(runtime.sent) == 2  # the ping reply and the denial were still delivered


@pytest.mark.asyncio
async def test_run_spawns_root_uniformly_and_returns_its_exit_code():
  dispatcher, runtime = make_dispatcher()
  runtime.next_peers.append('root')
  assert dispatcher.root is None  # unset until run() spawns it
  run_task = asyncio.ensure_future(dispatcher.run(_LAUNCH))
  await _settle()
  [(launch, timeout, exchange)] = runtime.spawns
  assert (launch, timeout) == (_LAUNCH, None)  # the root carries no request-lifecycle timeout
  assert dispatcher.root == 'root'
  assert dispatcher.exchanges[exchange].requester is None  # host-anchored
  assert dispatcher.workers['root'] == exchange
  dispatcher.on_exit('root', 7, '')
  assert await asyncio.wait_for(run_task, 5) == 7
  assert runtime.sent == []  # a host-anchored exchange closes silently on exit
  assert runtime._stopped.is_set()  # teardown ran


@pytest.mark.asyncio
async def test_stop_unblocks_a_running_run():
  dispatcher, runtime = make_dispatcher()
  runtime.next_peers.append('root')
  run_task = asyncio.ensure_future(dispatcher.run(_LAUNCH))
  await _settle()
  dispatcher.stop()
  assert await asyncio.wait_for(run_task, 5) == 0
  assert runtime._stopped.is_set()
