import asyncio
import contextlib
from pathlib import Path
from typing import cast

import pytest

from bro.bench import job
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.client import Client
from bro.broker.environment import BROKER_CHANNEL
from bro.broker.job import OUTPUT_DIRECTORY
from bro.broker.transport import ChannelID
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.quest import LAUNCH
from bro.worker_types import Host, LaunchDenied, LaunchRequest, PeerDescription, installed_type

TIMEOUT = 5.0
CONFIG = 'benchmark/bro/benchmark/job.yaml'
ROOT = 'root-peer'
REF = 'sha256:' + 'a' * 64


@pytest.fixture
def tree(tmp_path):
  (tmp_path / 'benchmark').mkdir()
  (tmp_path / 'benchmark' / 'pyproject.toml').write_text('[project]\n')
  config = tmp_path / CONFIG
  config.parent.mkdir(parents=True)
  config.write_text('datasets: []\n')
  return tmp_path


def _owner(tree: Path, *, depth: int = 0) -> PeerDescription:
  return PeerDescription(
    mission=ROOT,
    workspace='workspace',
    tree=tree,
    type='bro',
    bro='bro-dev',
    permits=frozenset(),
    member=None,
    expected=False,
    artifact_view=False,
    published_ports=(),
    depth=depth,
  )


def _request(tree: Path, args: dict, *, depth: int = 0) -> LaunchRequest:
  return LaunchRequest(
    id='R',
    type=job.BENCHMARK,
    args=args,
    owner=_owner(tree, depth=depth),
    requested_talk=frozenset(),
    timeout=job.DEFAULT_TIMEOUT,
    share=(),
    manual=False,
  )


def _worker_type() -> job.BenchmarkType:
  return job.BenchmarkType(cast(Host, object()))


def _denial(tree: Path, args: dict, *, depth: int = 0) -> str:
  with pytest.raises(LaunchDenied) as raised:
    _worker_type().launch(_request(tree, args, depth=depth))
  return str(raised.value)


class TestBenchmarkType:
  def test_installed_worker_type_resolves_to_the_benchmark(self):
    assert installed_type(job.BENCHMARK) is job.BenchmarkType

  def test_declarations_are_fixed_for_a_mute_host_job(self):
    worker_type = _worker_type()
    assert worker_type.name == 'benchmark'
    assert worker_type.permits == frozenset()
    assert worker_type.default_timeout == 12 * 3600
    assert worker_type.widens_talk is False
    assert worker_type.manual is False
    assert worker_type.talk(cast(LaunchRequest, object())) == frozenset()

  def test_root_request_builds_the_job(self, tree, monkeypatch):
    monkeypatch.setenv('BENCH_SENTINEL', 'yes')
    monkeypatch.setenv('HARBOR_API_KEY', 'ambient-key-outside-the-session-scope')
    monkeypatch.setenv('UV_PROJECT_ENVIRONMENT', '/wrong/shared-environment')
    monkeypatch.setenv('VIRTUAL_ENV', '/the/launcher/venv')

    run = _worker_type().launch(_request(tree, {'config': CONFIG}))

    assert run.command.command == (
      'uv',
      'run',
      '--project',
      str((tree / 'benchmark').resolve()),
      'bro.benchmark.job',
      '-c',
      str((tree / CONFIG).resolve()),
      '--jobs-dir',
      OUTPUT_DIRECTORY,
    )
    assert run.command.env['BENCH_SENTINEL'] == 'yes'
    assert 'HARBOR_API_KEY' not in run.command.env
    host_environment = Path(run.command.env['UV_PROJECT_ENVIRONMENT'])
    assert host_environment == tree.parent / 'benchmark-venv'
    assert not host_environment.is_relative_to(tree)
    assert 'VIRTUAL_ENV' not in run.command.env
    assert run.permits == frozenset()

  def test_non_root_owner_is_denied(self, tree):
    assert 'only the session root' in _denial(tree, {'config': CONFIG}, depth=1)

  def test_unknown_field_is_denied(self, tree):
    assert 'unknown benchmark field' in _denial(tree, {'config': CONFIG, 'upload': 'private'})

  def test_missing_config_is_denied(self, tree):
    assert "non-empty string 'config'" in _denial(tree, {})

  def test_absolute_config_is_denied(self, tree):
    assert 'relative to the workspace root' in _denial(tree, {'config': str(tree / CONFIG)})

  def test_config_escaping_the_workspace_is_denied(self, tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside') / 'job.yaml'
    outside.write_text('datasets: []\n')
    relative = '../' * 10 + str(outside).lstrip('/')
    assert 'escapes the workspace' in _denial(tree, {'config': relative})

  def test_absent_config_file_is_denied(self, tree):
    assert 'no job config' in _denial(tree, {'config': 'benchmark/nothing.yaml'})

  def test_workspace_without_benchmark_project_is_denied(self, tmp_path):
    config = tmp_path / 'job.yaml'
    config.write_text('datasets: []\n')
    assert 'no benchmark project' in _denial(tmp_path, {'config': 'job.yaml'})


def _result(request_id: str, payload: dict) -> Message:
  return Message(type=Tag.RESULT, payload=payload, request=request_id)


def test_await_outcome_logs_launch_only_for_started(caplog):
  request = Message(
    type=Tag.REQUEST,
    id='request',
    payload={'kind': LAUNCH, 'args': {'type': job.BENCHMARK}},
  )

  class FakeClient:
    def await_reply(self, sent, timeout, *, on_interim, timeout_after_interim):
      on_interim(brotocol.mark(sent.request_id, 'accepted'))
      on_interim(brotocol.mark(sent.request_id, 'started'))
      on_interim(brotocol.mark(sent.request_id, 'trail', trail_id='trail'))
      return brotocol.result(sent.request_id, 'ok', value={'ref': REF})

  assert job._await_outcome(cast(Client, FakeClient()), request, 10) == REF
  assert [record.message for record in caplog.records].count('benchmark job launched') == 1


class TestInterpretResult:
  def test_ok_returns_the_run_ref(self):
    message = _result('R', {'outcome': 'ok', 'value': {'ref': REF}})
    assert job._interpret_result(message) == REF

  def test_ok_without_a_run_raises(self):
    message = _result('R', {'outcome': 'ok', 'value': {}})
    with pytest.raises(job.JobError, match='no run'):
      job._interpret_result(message)

  def test_denied_raises_with_the_reason(self):
    message = _result('R', {'outcome': 'denied', 'error': 'not the root'})
    with pytest.raises(job.JobError, match='not the root'):
      job._interpret_result(message)

  def test_failed_exit_names_the_code_and_the_run(self):
    message = _result(
      'R', {'outcome': 'failed', 'detail': {'reason': 'exit', 'exit_code': 3, 'ref': REF}}
    )
    with pytest.raises(job.JobError, match=f'exit code 3.*{REF}'):
      job._interpret_result(message)

  def test_failed_timeout_names_the_reason(self):
    message = _result('R', {'outcome': 'failed', 'detail': {'reason': 'timeout'}})
    with pytest.raises(job.JobError, match='timeout'):
      job._interpret_result(message)


# --- the CLI over a live channel ---------------------------------------------------


class StubSink:
  """records inbound traffic onto asyncio queues the test coroutine can await."""

  def __init__(self):
    self.messages: asyncio.Queue = asyncio.Queue()  # (channel, message)

  async def on_connect(self, channel: ChannelID) -> None:
    pass

  async def on_message(self, channel: ChannelID, message: Message) -> None:
    self.messages.put_nowait((channel, message))

  async def on_disconnect(self, channel: ChannelID) -> None:
    pass


@contextlib.asynccontextmanager
async def running_server(monkeypatch):
  transport = TcpServerTransport([LOCAL_HOST])
  sink = StubSink()
  serve_task = asyncio.create_task(transport.serve(sink))
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  provisioned = await transport.provision()
  monkeypatch.setenv(BROKER_CHANNEL, provisioned.host_endpoint.address(LOCAL_HOST))
  try:
    yield transport, sink
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


@pytest.mark.asyncio
async def test_start_detach_sends_the_request_and_prints_its_id(monkeypatch, capsys):
  async with running_server(monkeypatch) as (transport, sink):
    argv = ['benchmark-job', 'start', '-c', CONFIG, '--timeout', '60', '--detach']
    task = asyncio.create_task(asyncio.to_thread(job.main, argv))
    channel, message = await asyncio.wait_for(sink.messages.get(), TIMEOUT)
    assert message.kind == LAUNCH
    assert message.args == {'type': job.BENCHMARK, 'config': CONFIG, 'timeout': 60.0}
    await transport.send(channel, brotocol.mark(message.id, 'accepted'))
    assert await task == 0
    assert capsys.readouterr().out.strip() == message.id


def test_start_without_a_channel_fails(monkeypatch, capsys, caplog):
  monkeypatch.delenv(BROKER_CHANNEL, raising=False)
  assert job.main(['benchmark-job', 'start', '-c', CONFIG]) == 1
  assert capsys.readouterr().out == ''
  assert any(BROKER_CHANNEL in record.getMessage() for record in caplog.records)


def test_check_help_has_no_conversation_cursor(capsys):
  with pytest.raises(SystemExit):
    job.main(['benchmark-job', 'check', '--help'])
  assert '--last-seen' not in capsys.readouterr().out


def test_check_timeout_without_wait_errors(monkeypatch, caplog):
  monkeypatch.setenv(BROKER_CHANNEL, 'tcp://token@127.0.0.1:1')
  assert job.main(['benchmark-job', 'check', 'R-1', '--timeout', '5']) == 1
  assert any('--timeout' in record.getMessage() for record in caplog.records)


def test_unknown_verb_is_a_usage_error(caplog):
  assert job.main(['benchmark-job']) == 2
  assert any('usage' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_detached_denial_fails_before_printing_an_id(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as (transport, sink):
    argv = ['benchmark-job', 'start', '-c', CONFIG, '--detach']
    task = asyncio.create_task(asyncio.to_thread(job.main, argv))
    channel, request = await asyncio.wait_for(sink.messages.get(), TIMEOUT)
    await transport.send(
      channel,
      brotocol.result(request.id, 'denied', error='benchmark denied'),
    )
    assert await task == 1
    assert capsys.readouterr().out == ''
    assert 'benchmark denied' in caplog.text


@pytest.mark.asyncio
async def test_check_reads_pending_and_terminal_journal_records(monkeypatch, capsys, caplog):
  async with running_server(monkeypatch) as (transport, sink):
    pending = asyncio.create_task(asyncio.to_thread(job.main, ['benchmark-job', 'check', 'JOB-1']))
    channel, query = await asyncio.wait_for(sink.messages.get(), TIMEOUT)
    assert query.kind == 'query'
    assert query.args == {'id': 'JOB-1'}
    await transport.send(
      channel,
      brotocol.result(
        query.id,
        'ok',
        value={
          'mission': {
            'id': 'JOB-1',
            'kind': 'launch',
            'type': 'benchmark',
            'parent': 'ROOT',
            'args': {'config': CONFIG},
            'state': 'started',
          }
        },
      ),
    )
    assert await pending == job.PENDING_EXIT_CODE
    assert 'still running' in caplog.text

    completed = asyncio.create_task(
      asyncio.to_thread(job.main, ['benchmark-job', 'check', 'JOB-1'])
    )
    channel, query = await asyncio.wait_for(sink.messages.get(), TIMEOUT)
    await transport.send(
      channel,
      brotocol.result(
        query.id,
        'ok',
        value={
          'mission': {
            'id': 'JOB-1',
            'kind': 'launch',
            'type': 'benchmark',
            'parent': 'ROOT',
            'args': {'config': CONFIG},
            'state': 'ended',
            'result': {'outcome': 'ok', 'value': {'ref': 'sha256:' + 'a' * 64}},
          }
        },
      ),
    )
    assert await completed == 0
    assert ('sha256:' + 'a' * 64) in capsys.readouterr().out
