import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from bro.broker import brotocol
from bro.broker.job import CommandJob
from bro.broker.runtime import Runtime
from bro.broker.spawn import LaunchSpec, Spawner
from bro.broker.supervisor import ExpectedSupervisor, JobSupervisor, SpawnedSupervisor, Supervisor
from bro.broker.supervisor_test_helper import FakeScheduler
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint

TIMEOUT = 5.0


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

  async def spawn(self, launch, provisioned, mission, talk):
    self.spawn_call = (launch, provisioned, mission)
    assert self.events is not None
    for message in self.launch_messages:
      self.events.on_message(message)
    return self.handle

  async def launch_job(self, command, directory):
    self.job_call = (command, directory)
    return self.handle

  async def close(self, peer):
    self.closed.append(peer)


class GatedRuntime(FakeRuntime):
  """a runtime whose launch completes only once the test releases it."""

  def __init__(self, tmp_path: Path):
    super().__init__(tmp_path)
    self.release = asyncio.Event()
    self.launched = []

  async def spawn(self, launch, provisioned, mission, talk):
    await self.release.wait()
    self.launched.append(mission)
    return self.handle


class Listener:
  def __init__(self):
    self.bound = []
    self.ready = []
    self.messages = []
    self.deaths = []

  def on_bound(self, supervisor, worker):
    self.bound.append(worker)

  def on_ready(self, supervisor):
    self.ready.append(supervisor.mission)

  def on_message(self, supervisor, message, *, from_supervisor):
    self.messages.append((message, from_supervisor))

  def on_death(self, supervisor, report):
    self.deaths.append(report)


async def _settle():
  for _ in range(20):
    await asyncio.sleep(0)


async def _until(condition: Callable[[], bool]) -> None:
  async with asyncio.timeout(TIMEOUT):
    while not condition():
      await asyncio.sleep(0.01)


def test_supervisor_passes_messages_for_other_missions_but_refuses_their_marks():
  listener = Listener()
  supervisor = Supervisor(cast(Runtime, object()), listener, 'own-mission', timeout=None)
  supervisor._mark_started()
  routed = brotocol.message('child-mission', {'text': 'steer'})

  supervisor.on_message(routed)
  supervisor.on_message(brotocol.mark('child-mission', 'trail', trail_id='wrong'))

  assert [message for message, _ in listener.messages] == [
    brotocol.mark('own-mission', 'started'),
    routed,
  ]


@pytest.mark.asyncio
async def test_spawned_supervisor_binds_before_launch_and_marks_started(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
  await _settle()
  assert listener.bound == ['worker-peer']
  assert runtime.spawn_call[2] == 'mission'
  assert listener.messages[0][0].payload == {'transition': 'started'}
  runtime.handle.exit.set_result(0)
  await _settle()
  assert listener.deaths[0].reason == 'exit'
  assert listener.deaths[0].exit_code == 0
  assert listener.deaths[0].output_tail == 'tail'


@pytest.mark.asyncio
async def test_a_supervisor_whose_deadline_cannot_be_armed_begins_nothing(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()

  def refuse(seconds, callback):
    raise OSError('no timers')

  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
    schedule=refuse,
  )
  with pytest.raises(OSError):
    supervisor.begin()
  await _settle()
  assert runtime.events is None
  assert listener.bound == []
  assert listener.deaths == []


@pytest.mark.asyncio
async def test_spawned_supervisor_folds_started_before_messages_sent_during_launch(tmp_path):
  runtime = FakeRuntime(tmp_path)
  runtime.launch_messages = [
    brotocol.mark('mission', 'trail', trail_id='trail'),
    brotocol.result('mission', 'ok'),
  ]
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
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
async def test_spawned_supervisor_drains_the_channel_before_reporting_exit(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
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
async def test_spawned_supervisor_warns_when_channel_drain_expires(tmp_path, monkeypatch, caplog):
  monkeypatch.setattr('bro.broker.supervisor._DRAIN_TIMEOUT', 0)
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
  await _settle()
  assert runtime.events is not None
  runtime.events.on_connect()

  with caplog.at_level(logging.WARNING):
    runtime.handle.exit.set_result(0)
    await _settle()

  assert listener.deaths[0].reason == 'exit'
  assert 'reporting exit without a complete drain' in caplog.text


@pytest.mark.asyncio
async def test_started_replaces_the_launch_bound_with_the_mission_timeout(tmp_path):
  runtime = FakeRuntime(tmp_path)
  schedule = FakeScheduler()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    Listener(),
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
    launch_timeout=30,
    schedule=schedule,
  )
  supervisor.begin()
  assert [(timer.seconds, timer.cancelled) for timer in schedule.timers] == [(30, False)]
  await _settle()
  assert [(timer.seconds, timer.cancelled) for timer in schedule.timers] == [
    (30, True),
    (10, False),
  ]
  runtime.handle.exit.set_result(0)
  await _settle()
  assert schedule.timers[1].cancelled


@pytest.mark.asyncio
async def test_launch_timeout_kills_a_handle_returned_after_cancellation(tmp_path):
  runtime = GatedRuntime(tmp_path)
  listener = Listener()
  schedule = FakeScheduler()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
    schedule=schedule,
  )
  supervisor.begin()
  await _settle()
  (launch_deadline,) = schedule.timers
  launch_deadline.fire()
  assert supervisor.ending
  assert runtime.launched == []
  runtime.release.set()
  await _until(lambda: listener.deaths != [])
  assert runtime.launched == ['mission']
  assert runtime.handle.killed
  assert listener.deaths[0].reason == 'timeout'


@pytest.mark.asyncio
async def test_spawned_supervisor_timeout_kills_the_process_and_reports_on_reap(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  schedule = FakeScheduler()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
    schedule=schedule,
  )
  supervisor.begin()
  await _settle()
  schedule.timers[-1].fire()
  await _until(lambda: listener.deaths != [])
  assert runtime.handle.killed
  assert listener.deaths[0].reason == 'timeout'


@pytest.mark.asyncio
async def test_expected_supervisor_defers_ready_then_marks_started_on_attach(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  provisioned = []
  supervisor = ExpectedSupervisor(cast(Runtime, runtime), listener, 'mission', provisioned.append)
  supervisor.begin()
  await _settle()
  assert provisioned[0].channel == 'worker-peer'
  assert listener.ready == ['mission']
  assert listener.messages == []
  assert runtime.events is not None
  runtime.events.on_connect()
  assert listener.messages[0][0].payload == {'transition': 'started'}
  runtime.events.on_disconnect()
  await _settle()
  assert listener.deaths[0].reason == 'disconnected'


@pytest.mark.asyncio
async def test_expected_supervisor_prepares_off_loop_before_ready(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  preparing = threading.Event()
  release = threading.Event()

  def ready(provisioned):
    del provisioned
    preparing.set()
    assert release.wait(5)

  supervisor = ExpectedSupervisor(cast(Runtime, runtime), listener, 'mission', ready)
  supervisor.begin()
  await _until(preparing.is_set)
  assert listener.ready == []
  await asyncio.sleep(0)
  release.set()
  await _until(lambda: listener.ready != [])
  assert listener.ready == ['mission']
  await supervisor.stop()


@pytest.mark.asyncio
async def test_expected_supervisor_kill_closes_only_its_host_channel(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = ExpectedSupervisor(
    cast(Runtime, runtime), listener, 'mission', lambda provisioned: None
  )
  supervisor.begin()
  await _settle()
  await supervisor.stop()
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


class StalledOutput(FakeOutput):
  """an output whose collection never completes."""

  def __init__(self, directory):
    super().__init__(directory)
    self.collecting = asyncio.Event()

  async def collect(self, directory, context, requester) -> dict:
    self.collecting.set()
    await asyncio.Event().wait()
    raise AssertionError('stalled collection resumed')


@pytest.mark.asyncio
async def test_job_output_open_failure_is_terminal(tmp_path):
  class RaisingOutput:
    def open(self):
      raise OSError('cannot open')

    async def collect(self, directory, context, requester):
      raise AssertionError('collect called after open failed')

  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = JobSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    CommandJob(('true',), {}),
    RaisingOutput(),
    None,
    'requester',
    timeout=10,
  )
  supervisor.begin()
  await _settle()
  assert listener.deaths[0].reason == 'output'
  assert listener.deaths[0].error == 'cannot open'


@pytest.mark.asyncio
async def test_job_supervisor_collects_clean_exit_as_success(tmp_path, monkeypatch):
  runtime = FakeRuntime(tmp_path)
  monkeypatch.setattr('bro.broker.supervisor.launch_job', runtime.launch_job)
  listener = Listener()
  output = FakeOutput(tmp_path / 'run')
  supervisor = JobSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    CommandJob(('true',), {}),
    output,
    'context',
    'requester',
    timeout=10,
  )
  supervisor.begin()
  await _settle()
  assert listener.bound == ['job:mission']
  runtime.handle.exit.set_result(0)
  await _until(lambda: listener.deaths != [])
  result = listener.messages[-1][0]
  assert result.payload == {'outcome': 'ok', 'value': {'ref': 'artifact'}}
  assert output.collected[0][1:] == ('context', 'requester')


@pytest.mark.asyncio
async def test_job_collection_failure_emits_failed_output_and_keeps_the_run(tmp_path, monkeypatch):
  class RaisingOutput(FakeOutput):
    async def collect(self, directory, context, requester) -> dict:
      raise OSError('collect broke')

  runtime = FakeRuntime(tmp_path)
  monkeypatch.setattr('bro.broker.supervisor.launch_job', runtime.launch_job)
  listener = Listener()
  output = RaisingOutput(tmp_path / 'run')
  supervisor = JobSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    CommandJob(('true',), {}),
    output,
    None,
    'requester',
    timeout=1,
  )
  supervisor.begin()
  await _settle()
  runtime.handle.exit.set_result(0)
  await _until(lambda: listener.deaths != [])
  result = listener.messages[-1][0]
  assert result.payload['detail']['reason'] == 'output'
  assert result.payload['error'] == 'collect broke'
  assert output.directory.is_dir()


@pytest.mark.asyncio
async def test_job_supervisor_carries_collected_output_on_failure(tmp_path, monkeypatch):
  runtime = FakeRuntime(tmp_path)
  monkeypatch.setattr('bro.broker.supervisor.launch_job', runtime.launch_job)
  listener = Listener()
  output = FakeOutput(tmp_path / 'run')
  supervisor = JobSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    CommandJob(('false',), {}),
    output,
    None,
    'requester',
    timeout=10,
  )
  supervisor.begin()
  await _settle()
  runtime.handle.exit.set_result(3)
  await _until(lambda: listener.deaths != [])
  result = listener.messages[-1][0]
  assert result.payload['outcome'] == 'failed'
  assert result.payload['detail']['reason'] == 'exit'
  assert result.payload['detail']['exit_code'] == 3
  assert result.payload['detail']['ref'] == 'artifact'


@pytest.mark.asyncio
async def test_spawned_supervisor_end_kills_the_process_and_reports_the_reason_on_reap(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
  await _settle()

  supervisor.end('cancelled')
  supervisor.end('orphaned')
  await _settle()

  assert runtime.handle.killed
  assert [report.reason for report in listener.deaths] == ['cancelled']
  assert listener.deaths[0].exit_code == -15
  assert listener.deaths[0].output_tail == 'tail'


@pytest.mark.asyncio
async def test_end_before_start_reports_only_once_the_late_handle_is_reaped(tmp_path):
  runtime = GatedRuntime(tmp_path)
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
  await _settle()

  supervisor.end('orphaned')
  await _settle()

  assert supervisor.ending
  assert listener.deaths == []
  assert not runtime.handle.killed
  runtime.release.set()
  await _until(lambda: listener.deaths != [])
  assert runtime.handle.killed
  assert [report.reason for report in listener.deaths] == ['orphaned']
  assert not supervisor.ending


@pytest.mark.asyncio
async def test_end_before_start_outranks_the_failure_of_the_interrupted_launch(tmp_path):
  class FailingRuntime(GatedRuntime):
    async def spawn(self, launch, provisioned, mission, talk):
      await super().spawn(launch, provisioned, mission, talk)
      raise RuntimeError('launch broke')

  runtime = FailingRuntime(tmp_path)
  listener = Listener()
  supervisor = SpawnedSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    LaunchSpec(),
    cast(Spawner, runtime),
    talk=frozenset(),
    timeout=10,
  )
  supervisor.begin()
  await _settle()

  supervisor.end('cancelled')
  await _settle()

  assert listener.deaths == []
  runtime.release.set()
  await _until(lambda: listener.deaths != [])
  assert [(report.reason, report.error) for report in listener.deaths] == [
    ('cancelled', 'launch broke'),
  ]


@pytest.mark.asyncio
async def test_expected_supervisor_end_during_preparation_reports_once_it_has_completed(tmp_path):
  effects: list[str] = []
  seen_at_death: list[list[str]] = []

  class RecordingListener(Listener):
    def on_death(self, supervisor, report):
      super().on_death(supervisor, report)
      seen_at_death.append(list(effects))

  runtime = FakeRuntime(tmp_path)
  listener = RecordingListener()
  preparing = threading.Event()
  release = threading.Event()

  def ready(provisioned):
    del provisioned
    preparing.set()
    assert release.wait(5)
    effects.append('token written')

  supervisor = ExpectedSupervisor(cast(Runtime, runtime), listener, 'mission', ready)
  supervisor.begin()
  await _until(preparing.is_set)

  supervisor.end('cancelled')
  await _settle()

  assert listener.deaths == []
  release.set()
  await _until(lambda: listener.deaths != [])
  assert [report.reason for report in listener.deaths] == ['cancelled']
  assert seen_at_death == [['token written']]
  assert listener.ready == []


@pytest.mark.asyncio
async def test_expected_supervisor_end_reports_the_reason_instead_of_disconnected(tmp_path):
  runtime = FakeRuntime(tmp_path)
  listener = Listener()
  supervisor = ExpectedSupervisor(
    cast(Runtime, runtime), listener, 'mission', lambda provisioned: None
  )
  supervisor.begin()
  await _settle()
  assert runtime.events is not None
  runtime.events.on_connect()

  supervisor.end('cancelled')
  await _settle()

  assert [report.reason for report in listener.deaths] == ['cancelled']


@pytest.mark.asyncio
async def test_job_supervisor_end_reports_the_reason_with_its_collected_output(
  tmp_path, monkeypatch
):
  runtime = FakeRuntime(tmp_path)
  monkeypatch.setattr('bro.broker.supervisor.launch_job', runtime.launch_job)
  listener = Listener()
  output = FakeOutput(tmp_path / 'run')
  supervisor = JobSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    CommandJob(('sleep', '60'), {}),
    output,
    None,
    'requester',
    timeout=10,
  )
  supervisor.begin()
  await _settle()

  supervisor.end('cancelled')
  await _until(lambda: listener.deaths != [])

  assert runtime.handle.killed
  result = listener.messages[-1][0]
  assert result.payload['outcome'] == 'failed'
  assert result.payload['detail'] == {'reason': 'cancelled', 'exit_code': -15, 'ref': 'artifact'}
  assert [report.reason for report in listener.deaths] == ['cancelled']


@pytest.mark.asyncio
async def test_job_supervisor_end_during_collection_reports_the_reason_and_keeps_the_run(
  tmp_path, monkeypatch
):
  runtime = FakeRuntime(tmp_path)
  monkeypatch.setattr('bro.broker.supervisor.launch_job', runtime.launch_job)
  listener = Listener()
  output = StalledOutput(tmp_path / 'run')
  supervisor = JobSupervisor(
    cast(Runtime, runtime),
    listener,
    'mission',
    CommandJob(('true',), {}),
    output,
    None,
    'requester',
    timeout=10,
  )
  supervisor.begin()
  await _settle()
  runtime.handle.exit.set_result(0)
  await asyncio.wait_for(output.collecting.wait(), TIMEOUT)

  supervisor.end('orphaned')
  await _settle()

  assert [report.reason for report in listener.deaths] == ['orphaned']
  assert listener.deaths[0].exit_code == 0
  assert output.directory.is_dir()
