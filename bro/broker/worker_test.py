import asyncio
from pathlib import Path
from typing import cast

import pytest

from bro.broker import brotocol
from bro.broker.job import CommandJob
from bro.broker.runtime import Runtime
from bro.broker.spawn import LaunchSpec
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.broker.worker import ExpectedWorker, JobWorker, SpawnedWorker


class FakeHandle:
  def __init__(self):
    self.exit = asyncio.get_running_loop().create_future()
    self.killed = False

  async def wait(self):
    return await self.exit

  async def kill(self):
    self.killed = True
    if not self.exit.done():
      self.exit.set_result(-15)

  def output_tail(self):
    return 'tail'


class FakeRuntime:
  def __init__(self, tmp_path: Path):
    self.events = None
    self.handle = FakeHandle()
    self.closed = []
    self.tmp_path = tmp_path
    self.launch_messages = []

  async def provision(self, events):
    self.events = events
    return Provisioned('worker-peer', Endpoint(1234, 'token'))

  async def launch(self, launch, provisioned, quest):
    self.launch_call = (launch, provisioned, quest)
    assert self.events is not None
    for message in self.launch_messages:
      self.events.on_message(message)
    return self.handle

  async def launch_job(self, command, directory):
    self.job_call = (command, directory)
    return self.handle

  async def close(self, peer):
    self.closed.append(peer)


class Listener:
  def __init__(self):
    self.bound = []
    self.ready = []
    self.messages = []
    self.deaths = []

  def on_worker_bound(self, worker, peer):
    self.bound.append(peer)

  def on_worker_ready(self, worker):
    self.ready.append(worker.quest)

  def on_worker_message(self, worker, message, *, host_worker):
    self.messages.append((message, host_worker))

  def on_worker_death(self, worker, report):
    self.deaths.append(report)


async def _settle():
  for _ in range(20):
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_spawned_worker_binds_before_launch_and_marks_started(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  worker = SpawnedWorker(cast(Runtime, runtime), listener, 'quest', LaunchSpec(), timeout=10)
  worker.begin()
  await _settle()
  assert listener.bound == ['worker-peer']
  assert runtime.launch_call[2] == 'quest'
  assert listener.messages[0][0].payload == {'transition': 'started'}
  runtime.handle.exit.set_result(0)
  await _settle()
  assert listener.deaths[0].reason == 'exit'
  assert listener.deaths[0].exit_code == 0
  assert listener.deaths[0].output_tail == 'tail'


@pytest.mark.asyncio
async def test_spawned_worker_folds_started_before_messages_sent_during_launch(tmp_path):
  runtime = FakeRuntime(tmp_path)
  runtime.launch_messages = [
    brotocol.mark('quest', 'trail', trail_id='trail'),
    brotocol.result('quest', 'ok'),
  ]
  listener = Listener()
  worker = SpawnedWorker(cast(Runtime, runtime), listener, 'quest', LaunchSpec(), timeout=10)
  worker.begin()
  await _settle()
  assert [message.payload for message, _ in listener.messages] == [
    {'transition': 'started'},
    {'transition': 'trail', 'trail_id': 'trail'},
    {'outcome': 'ok'},
  ]
  assert [host for _, host in listener.messages] == [True, False, False]
  runtime.handle.exit.set_result(0)
  await _settle()


@pytest.mark.asyncio
async def test_spawned_worker_drains_the_channel_before_reporting_exit(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  worker = SpawnedWorker(cast(Runtime, runtime), listener, 'quest', LaunchSpec(), timeout=10)
  worker.begin()
  await _settle()
  assert runtime.events is not None
  runtime.events.on_connect()
  runtime.handle.exit.set_result(0)
  await _settle()
  assert listener.deaths == []
  runtime.events.on_disconnect()
  await _settle()
  assert listener.deaths[0].reason == 'exit'


@pytest.mark.asyncio
async def test_launch_timeout_kills_a_handle_returned_after_cancellation(tmp_path):
  class DelayedRuntime(FakeRuntime):
    def __init__(self, path):
      super().__init__(path)
      self.launched = []

    async def launch(self, launch, provisioned, quest):
      await asyncio.sleep(0.02)
      self.launched.append(quest)
      return self.handle

  runtime = DelayedRuntime(tmp_path)
  listener = Listener()
  worker = SpawnedWorker(
    cast(Runtime, runtime),
    listener,
    'quest',
    LaunchSpec(),
    timeout=10,
    launch_timeout=0.001,
  )
  worker.begin()
  await asyncio.sleep(0.05)
  assert runtime.launched == ['quest']
  assert runtime.handle.killed
  assert listener.deaths[0].reason == 'timeout'


@pytest.mark.asyncio
async def test_spawned_worker_timeout_kills_the_process_and_reports_on_reap(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  worker = SpawnedWorker(cast(Runtime, runtime), listener, 'quest', LaunchSpec(), timeout=0.001)
  worker.begin()
  await asyncio.sleep(0.01)
  await _settle()
  assert runtime.handle.killed
  assert listener.deaths[0].reason == 'timeout'


@pytest.mark.asyncio
async def test_expected_worker_defers_ready_then_marks_started_on_attach(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  provisioned = []
  worker = ExpectedWorker(cast(Runtime, runtime), listener, 'quest', provisioned.append)
  worker.begin()
  await _settle()
  assert provisioned[0].channel == 'worker-peer'
  assert listener.ready == ['quest']
  assert listener.messages == []
  assert runtime.events is not None
  runtime.events.on_connect()
  assert listener.messages[0][0].payload == {'transition': 'started'}
  runtime.events.on_disconnect()
  await _settle()
  assert listener.deaths[0].reason == 'disconnected'


@pytest.mark.asyncio
async def test_expected_worker_kill_closes_only_its_host_channel(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  worker = ExpectedWorker(cast(Runtime, runtime), listener, 'quest', lambda provisioned: None)
  worker.begin()
  await _settle()
  await worker.stop()
  assert runtime.closed == ['worker-peer']
  assert not runtime.handle.killed


class FakeOutput:
  def __init__(self, directory):
    self.directory = directory
    self.collected = []

  def open(self):
    self.directory.mkdir()
    return self.directory

  async def collect(self, directory, context, requester):
    self.collected.append((directory, context, requester))
    return {'ref': 'artifact'}


@pytest.mark.asyncio
async def test_job_output_open_failure_is_terminal(tmp_path):
  class RaisingOutput:
    def open(self):
      raise OSError('cannot open')

    async def collect(self, directory, context, requester):
      raise AssertionError('collect called after open failed')

  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  worker = JobWorker(
    cast(Runtime, runtime),
    listener,
    'quest',
    CommandJob(('true',), {}),
    RaisingOutput(),
    None,
    'requester',
    timeout=10,
  )
  worker.begin()
  await _settle()
  assert listener.deaths[0].reason == 'output'
  assert listener.deaths[0].error == 'cannot open'


@pytest.mark.asyncio
async def test_job_worker_collects_clean_exit_as_success(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  output = FakeOutput(tmp_path / 'run')
  worker = JobWorker(
    cast(Runtime, runtime),
    listener,
    'quest',
    CommandJob(('true',), {}),
    output,
    'context',
    'requester',
    timeout=10,
  )
  worker.begin()
  await _settle()
  assert listener.bound == ['job:quest']
  runtime.handle.exit.set_result(0)
  await asyncio.sleep(0.05)
  result = listener.messages[-1][0]
  assert result.payload == {'outcome': 'ok', 'value': {'ref': 'artifact'}}
  assert output.collected[0][1:] == ('context', 'requester')


@pytest.mark.asyncio
async def test_job_collection_failure_emits_failed_output_and_keeps_the_run(tmp_path):
  class RaisingOutput(FakeOutput):
    async def collect(self, directory, context, requester) -> dict:
      raise OSError('collect broke')

  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  output = RaisingOutput(tmp_path / 'run')
  worker = JobWorker(
    cast(Runtime, runtime),
    listener,
    'quest',
    CommandJob(('true',), {}),
    output,
    None,
    'requester',
    timeout=1,
  )
  worker.begin()
  await _settle()
  runtime.handle.exit.set_result(0)
  await asyncio.sleep(0.05)
  result = listener.messages[-1][0]
  assert result.payload['detail']['reason'] == 'output'
  assert result.payload['error'] == 'collect broke'
  assert output.directory.is_dir()


@pytest.mark.asyncio
async def test_job_collection_is_bounded_by_the_quest_deadline(tmp_path):
  class StalledOutput(FakeOutput):
    async def collect(self, directory, context, requester) -> dict:
      await asyncio.Event().wait()
      raise AssertionError('stalled collection resumed')

  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  output = StalledOutput(tmp_path / 'run')
  worker = JobWorker(
    cast(Runtime, runtime),
    listener,
    'quest',
    CommandJob(('true',), {}),
    output,
    None,
    'requester',
    timeout=0.01,
  )
  worker.begin()
  await _settle()
  runtime.handle.exit.set_result(0)
  await asyncio.sleep(0.05)
  assert listener.deaths[-1].reason == 'timeout'
  assert output.directory.is_dir()


@pytest.mark.asyncio
async def test_job_worker_carries_collected_output_on_failure(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  output = FakeOutput(tmp_path / 'run')
  worker = JobWorker(
    cast(Runtime, runtime),
    listener,
    'quest',
    CommandJob(('false',), {}),
    output,
    None,
    'requester',
    timeout=10,
  )
  worker.begin()
  await _settle()
  runtime.handle.exit.set_result(3)
  await asyncio.sleep(0.05)
  result = listener.messages[-1][0]
  assert result.payload['outcome'] == 'failed'
  assert result.payload['detail']['reason'] == 'exit'
  assert result.payload['detail']['exit_code'] == 3
  assert result.payload['detail']['ref'] == 'artifact'
