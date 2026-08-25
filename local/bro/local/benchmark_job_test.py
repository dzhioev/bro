import asyncio
import contextlib
import importlib.metadata
import json
from pathlib import Path
from typing import Optional, cast
from unittest.mock import Mock

import pytest

from bro.broker.brotocol import Message, Tag
from bro.broker.client import CHANNEL_ENV
from bro.broker.dispatcher import Dispatcher
from bro.broker.job import OUTPUT_DIRECTORY, CommandJob
from bro.broker.transport import ChannelID
from bro.broker.transports.tcp import LOCAL_HOST, TcpServerTransport
from bro.kinds import ArtifactResolver, KindContext
from bro.local import benchmark_job

TIMEOUT = 5.0
CONFIG = 'benchmark/bro/benchmark/job.yaml'
ROOT = 'root-peer'
REF = 'sha256:' + 'a' * 64


def _kind(tree: Path, credential_scope=frozenset()):
  return benchmark_job.benchmark_kind(
    KindContext(
      workspace_tree=tree,
      artifacts=cast(ArtifactResolver, object()),
      credential_scope=credential_scope,
    )
  )


@pytest.fixture
def tree(tmp_path):
  (tmp_path / 'benchmark').mkdir()
  (tmp_path / 'benchmark' / 'pyproject.toml').write_text('[project]\n')
  config = tmp_path / CONFIG
  config.parent.mkdir(parents=True)
  config.write_text('datasets: []\n')
  return tmp_path


class FakeContext:
  """the Dispatcher surface `benchmark_kind`'s handler drives: root exposure,
  the denial reply, and the job launch."""

  def __init__(self, root: Optional[str] = ROOT):
    self.root = root
    self.replies: list[tuple[str, dict]] = []
    self.jobs: list[tuple[CommandJob, str, Optional[float]]] = []

  def reply(self, peer, payload):
    self.replies.append((peer, payload))

  def job(self, command, requester, *, timeout=None):
    self.jobs.append((command, requester, timeout))


def _request(args: dict) -> Message:
  return Message(
    type=Tag.REQUEST,
    id='R',
    payload={'kind': 'benchmark', 'args': {'upload': 'none', **args}},
  )


def _denial(tree, args, *, peer=ROOT, context=None) -> str:
  context = context if context is not None else FakeContext()
  _kind(tree)(cast(Dispatcher, context), peer, _request(args))
  assert context.jobs == []
  [(target, payload)] = context.replies
  assert target == peer
  assert payload['outcome'] == 'denied'
  return payload['error']


class TestBenchmarkKind:
  def test_root_request_starts_the_job(self, tree, monkeypatch):
    monkeypatch.setenv('BENCH_SENTINEL', 'yes')
    monkeypatch.setenv('HARBOR_API_KEY', 'ambient-key-outside-the-session-scope')
    monkeypatch.setenv('UV_PROJECT_ENVIRONMENT', '/wrong/shared-environment')
    context = FakeContext()
    handle = _kind(tree)
    handle(cast(Dispatcher, context), ROOT, _request({'config': CONFIG}))
    assert context.replies == []
    [(command, requester, timeout)] = context.jobs
    assert command.command == (
      'uv',
      'run',
      '--project',
      str((tree / 'benchmark').resolve()),
      'bro.benchmark.job',
      '-c',
      str((tree / CONFIG).resolve()),
      '--jobs-dir',
      OUTPUT_DIRECTORY,
      '--upload',
      'none',
    )
    assert command.env['BENCH_SENTINEL'] == 'yes'  # the host environment rides the job
    assert 'HARBOR_API_KEY' not in command.env
    host_environment = Path(command.env['UV_PROJECT_ENVIRONMENT'])
    assert host_environment == tree.parent / 'benchmark-venv'
    assert not host_environment.is_relative_to(tree)
    assert (requester, timeout) == (ROOT, benchmark_job.DEFAULT_TIMEOUT)

  def test_request_timeout_bounds_the_job(self, tree):
    context = FakeContext()
    handle = _kind(tree)
    handle(cast(Dispatcher, context), ROOT, _request({'config': CONFIG, 'timeout': 60}))
    [(_, _, timeout)] = context.jobs
    assert timeout == 60.0

  def test_upload_without_a_granted_harbor_credential_is_denied(self, tree):
    error = _denial(tree, {'config': CONFIG, 'upload': 'private'})
    assert "secret 'harbor' not found" in error

  def test_upload_hydrates_the_scoped_harbor_key_into_the_host_job(self, tree, monkeypatch):
    store = Mock()
    store.get_instance.return_value = 'sk-harbor-scoped'
    monkeypatch.setattr(benchmark_job.credentials, 'default_store', lambda: store)
    context = FakeContext()

    _kind(tree, frozenset({'harbor+benchmark'}))(
      cast(Dispatcher, context),
      ROOT,
      _request({'config': CONFIG, 'upload': 'public'}),
    )

    [(command, _, _)] = context.jobs
    assert command.env['HARBOR_API_KEY'] == 'sk-harbor-scoped'
    store.get_instance.assert_called_once_with('harbor+benchmark')

  def test_non_root_peer_is_denied(self, tree):
    error = _denial(tree, {'config': CONFIG}, peer='child-peer')
    assert 'only the session root' in error

  def test_unset_root_is_denied(self, tree):
    error = _denial(tree, {'config': CONFIG}, context=FakeContext(root=None))
    assert 'only the session root' in error

  def test_unknown_field_is_denied(self, tree):
    assert 'unknown benchmark field' in _denial(tree, {'config': CONFIG, 'timout': 5})

  def test_missing_config_is_denied(self, tree):
    assert "non-empty string 'config'" in _denial(tree, {})

  def test_bad_timeout_is_denied(self, tree):
    assert 'positive number' in _denial(tree, {'config': CONFIG, 'timeout': 0})

  def test_bad_upload_visibility_is_denied(self, tree):
    assert "'upload' must be one of" in _denial(tree, {'config': CONFIG, 'upload': 'open'})

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


class TestInterpretResult:
  def test_ok_returns_the_run_ref(self):
    message = _result('R', {'outcome': 'ok', 'value': {'ref': REF}})
    assert benchmark_job._interpret_result(message) == REF

  def test_ok_without_a_run_raises(self):
    message = _result('R', {'outcome': 'ok', 'value': {}})
    with pytest.raises(benchmark_job.JobError, match='no run'):
      benchmark_job._interpret_result(message)

  def test_denied_raises_with_the_reason(self):
    message = _result('R', {'outcome': 'denied', 'error': 'not the root'})
    with pytest.raises(benchmark_job.JobError, match='not the root'):
      benchmark_job._interpret_result(message)

  def test_failed_exit_names_the_code_and_the_run(self):
    message = _result(
      'R', {'outcome': 'failed', 'detail': {'reason': 'exit', 'exit_code': 3, 'ref': REF}}
    )
    with pytest.raises(benchmark_job.JobError, match=f'exit code 3.*{REF}'):
      benchmark_job._interpret_result(message)

  def test_failed_timeout_names_the_reason(self):
    message = _result('R', {'outcome': 'failed', 'detail': {'reason': 'timeout'}})
    with pytest.raises(benchmark_job.JobError, match='timeout'):
      benchmark_job._interpret_result(message)


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
  monkeypatch.setenv(CHANNEL_ENV, provisioned.host_endpoint.address(LOCAL_HOST))
  try:
    yield sink
  finally:
    await transport.shutdown()
    await asyncio.wait_for(serve_task, TIMEOUT)


@pytest.mark.asyncio
async def test_start_detach_sends_the_request_and_prints_its_id(monkeypatch, capsys):
  async with running_server(monkeypatch) as sink:
    argv = ['benchmark-job', 'start', '-c', CONFIG, '--timeout', '60', '--detach']
    assert await asyncio.to_thread(benchmark_job.main, argv) == 0
    _, message = await asyncio.wait_for(sink.messages.get(), TIMEOUT)
    assert message.kind == benchmark_job.BENCHMARK
    assert message.args == {'config': CONFIG, 'upload': 'none', 'timeout': 60.0}
    assert capsys.readouterr().out.strip() == message.id


def test_start_without_a_channel_fails(monkeypatch, capsys, caplog):
  monkeypatch.delenv(CHANNEL_ENV, raising=False)
  assert benchmark_job.main(['benchmark-job', 'start', '-c', CONFIG]) == 1
  assert capsys.readouterr().out == ''
  assert any(CHANNEL_ENV in record.getMessage() for record in caplog.records)


def test_check_last_seen_with_wait_errors(monkeypatch, capsys, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://token@127.0.0.1:1')
  assert benchmark_job.main(['benchmark-job', 'check', 'R-1', '--wait', '--last-seen', '0']) == 1
  assert capsys.readouterr().out == ''
  assert any('--wait' in record.getMessage() for record in caplog.records)


def test_check_timeout_without_wait_errors(monkeypatch, caplog):
  monkeypatch.setenv(CHANNEL_ENV, 'tcp://token@127.0.0.1:1')
  assert benchmark_job.main(['benchmark-job', 'check', 'R-1', '--timeout', '5']) == 1
  assert any('--timeout' in record.getMessage() for record in caplog.records)


def test_unknown_verb_is_a_usage_error(caplog):
  assert benchmark_job.main(['benchmark-job']) == 2
  assert any('usage' in record.getMessage() for record in caplog.records)


def test_uploaded_job_url_reads_the_pipeline_record(tmp_path):
  record = tmp_path / OUTPUT_DIRECTORY / 'job' / benchmark_job.UPLOAD_RECORD
  record.parent.mkdir(parents=True)
  record.write_text(json.dumps({'visibility': 'public', 'url': 'https://hub.example/jobs/1'}))

  assert benchmark_job.uploaded_job_url(tmp_path) == 'https://hub.example/jobs/1'


def test_no_upload_record_means_the_run_was_not_published(tmp_path):
  assert benchmark_job.uploaded_job_url(tmp_path) is None


def test_the_local_distribution_contributes_the_harbor_credential_kind():
  entries = importlib.metadata.entry_points(group='bro.credentials', name='harbor')

  [entry] = entries
  assert entry.value == 'bro.local.credentials:HARBOR'
  assert entry.load() == {
    'sources': [{'file': 'harbor_api_key'}],
    'install': {'env': {'HARBOR_API_KEY': {'secret': '{{insert #name}}'}}},
  }
