import asyncio
import errno
import json
import logging
import os
import signal
from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from bro.files.confinement import SandboxLimits
from bro.files.protocol import MAX_REPLY_BYTES, Job, encode_frame
from bro.files.runner import SubprocessRunner
from bro.files.types import Limits, View
from bro.files.types_test_helper import make_type


class Payload(BaseModel):
  model_config = ConfigDict(extra='forbid')
  value: Any


def job(tmp_path, action='success', *, provider='provider', snapshot=None, **arguments):
  file_type = make_type(
    tmp_path,
    details=Payload,
    views=(View('text', 'Read text', Payload, True),),
    provider=f'bro.files.runner_test_helper:{provider}',
    limits=Limits(32 * 1024**2, 5000, 1000000, 20000),
  )
  return Job(
    file_type, 'info', {}, snapshot or json.dumps({'action': action, **arguments}).encode()
  )


@pytest.mark.asyncio
async def test_success_and_view_use_real_provider_and_declared_models(tmp_path):
  runner = SubprocessRunner()
  request = job(tmp_path)
  reply = await runner.run(request)
  assert reply.ok == {'value': 'Здравствуйте'}
  assert reply.engine == 'test-engine 1'
  assert reply.sandbox['seccomp'] is True
  viewed = await runner.run(
    replace(request, operation='view', arguments={'view': 'text', 'pages': [2]})
  )
  assert viewed.ok['value'] == {'view': 'text', 'pages': [2], 'size': len(request.snapshot)}


@pytest.mark.asyncio
async def test_invalid_input_is_a_caller_fault(tmp_path):
  with pytest.raises(ValueError, match='document has no pages'):
    await SubprocessRunner().run(job(tmp_path, 'invalid'))


@pytest.mark.asyncio
async def test_engine_failure_after_confinement_keeps_traceback_in_escaped_log(tmp_path, caplog):
  with (
    caplog.at_level(logging.INFO),
    pytest.raises(RuntimeError, match='file engine failure') as error,
  ):
    await SubprocessRunner().run(job(tmp_path, 'engine'))
  assert 'private document' not in str(error.value)
  assert 'LookupError' in caplog.text and 'private document content' in caplog.text
  assert '\\n' in caplog.text
  assert all(
    field in caplog.text for field in ('operation=info', 'type=sample', 'sha256=', 'duration=')
  )


@pytest.mark.asyncio
@pytest.mark.parametrize('action,name', [('segfault', 'SIGSEGV'), ('abort', 'SIGABRT')])
async def test_native_crashes_are_named_and_reaped(tmp_path, action, name):
  with pytest.raises(RuntimeError, match=name):
    await SubprocessRunner().run(job(tmp_path, action))


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['stdout', 'stderr', 'reply_flood'])
async def test_output_floods_are_bounded_while_a_large_request_is_written(tmp_path, action):
  data = json.dumps({'action': action}).encode()
  data += b' ' * (32 * 1024**2 - len(data))
  with pytest.raises(RuntimeError, match='byte limit'):
    await SubprocessRunner().run(job(tmp_path, snapshot=data, provider='concurrent_output'))


@pytest.mark.asyncio
async def test_engine_fd1_output_does_not_enter_the_reply(tmp_path):
  assert (await SubprocessRunner().run(job(tmp_path, 'print'))).ok['value'] == 'Здравствуйте'


@pytest.mark.asyncio
async def test_full_sized_request_and_preparation_output_do_not_deadlock(tmp_path):
  data = b'{"action":"success"}'
  data += b' ' * (32 * 1024**2 - len(data))
  assert (
    await SubprocessRunner().run(job(tmp_path, provider='concurrent_output', snapshot=data))
  ).ok


@pytest.mark.asyncio
async def test_early_exit_is_judged_by_reply_not_incomplete_request_write(tmp_path):
  with pytest.raises(ValueError, match='early refusal'):
    await SubprocessRunner().run(
      job(tmp_path, provider='early_exit', snapshot=b'x' * (32 * 1024**2))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'data',
  [
    b'',
    b'x',
    (MAX_REPLY_BYTES + 1).to_bytes(8, 'big'),
    encode_frame({'ok': {}}),
    encode_frame({'error': {'kind': 'alien', 'message': 'bad'}}),
    encode_frame({'error': {'kind': 'engine', 'message': '\x00'}}),
    encode_frame({'error': {'kind': 'engine', 'message': 'bad'}}) + b'trailing',
  ],
)
async def test_malformed_and_trailing_replies_are_rejected(tmp_path, data):
  with pytest.raises(RuntimeError, match='invalid file reply|byte limit'):
    await SubprocessRunner().run(job(tmp_path, 'raw', hex=data.hex()))


@pytest.mark.asyncio
async def test_signal_after_a_frame_is_not_accepted(tmp_path):
  data = encode_frame({'error': {'kind': 'invalid_input', 'message': 'not successful'}})
  with pytest.raises(RuntimeError, match='SIGABRT'):
    await SubprocessRunner().run(job(tmp_path, 'raw', hex=data.hex(), signal=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['bad_model', 'bad_string'])
async def test_provider_results_are_validated_in_parent(tmp_path, action):
  with pytest.raises(RuntimeError, match='invalid file reply'):
    await SubprocessRunner().run(job(tmp_path, action))


@pytest.mark.asyncio
async def test_empty_environment_and_only_standard_descriptors_at_prepare(tmp_path, monkeypatch):
  monkeypatch.setenv('WORKER_SECRET', 'not inherited')
  with open('/dev/null', 'rb') as extra:
    os.set_inheritable(extra.fileno(), True)
    reply = await SubprocessRunner().run(job(tmp_path, 'environment'))
  value = reply.ok['value']
  # CPython's locale coercion can add LC_CTYPE even to an empty exec environment.
  assert value['environment'].keys() <= {'LC_CTYPE'}
  assert value['descriptors'] == [0, 1, 2]


class ObservedRunner(SubprocessRunner):
  def __init__(self, limits=None):
    super().__init__(limits)
    self.ready = asyncio.Event()
    self.process = None

  async def _exchange(self, worker, job, stdout, stderr):
    self.process = worker.process
    await super()._exchange(worker, job, stdout, stderr)

  def _read(self, descriptor, output, maximum, label):
    result = super()._read(descriptor, output, maximum, label)
    if label == 'stderr' and b'worker ready\n' in output:
      self.ready.set()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['cancel', 'close'])
async def test_cancellation_and_close_kill_and_reap_the_running_worker(tmp_path, action):
  runner = ObservedRunner()
  task = asyncio.create_task(runner.run(job(tmp_path, 'hold')))
  async with asyncio.timeout(10):
    await runner.ready.wait()
    if action == 'cancel':
      task.cancel()
      with pytest.raises(asyncio.CancelledError):
        await task
    else:
      runner.close()
      with pytest.raises(RuntimeError, match='SIGKILL'):
        await task
  assert runner.process is not None
  assert runner.process.returncode == -signal.SIGKILL
  assert not runner._workers
  with pytest.raises(ChildProcessError):
    os.waitpid(runner.process.pid, os.WNOHANG)
  if action == 'close':
    with pytest.raises(RuntimeError, match='closed'):
      await runner.run(job(tmp_path))


@pytest.mark.asyncio
async def test_wall_bound_kills_a_hang(tmp_path):
  with pytest.raises(RuntimeError, match='wall clock exceeded'):
    await SubprocessRunner(SandboxLimits(wall_seconds=0.5)).run(job(tmp_path, 'hold'))


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/etc/passwd', '/proc/self/environ'])
async def test_real_worker_cannot_open_host_files(tmp_path, path):
  result = await SubprocessRunner().run(
    job(tmp_path, 'probe', probe={'action': 'open', 'path': path})
  )
  assert result.ok == {'value': {'errno': errno.EACCES}}


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['socket', 'fork', 'thread', 'exec', 'setsid', 'raise_limit'])
async def test_real_worker_cannot_escape_confinement(tmp_path, action):
  with pytest.raises(RuntimeError, match='SIGSYS'):
    await SubprocessRunner().run(job(tmp_path, 'probe', probe={'action': action, 'family': 2}))


@pytest.mark.parametrize('mode', ['parent_death', 'cancel_group', 'exit_with_open_pipes'])
def test_process_lifecycle_uses_exit_and_kills_orphans_and_groups(mode):
  import subprocess
  import sys

  process = subprocess.run(
    [sys.executable, '-m', 'bro.files.runner_test_helper', mode],
    capture_output=True,
    timeout=15,
  )
  assert process.returncode == 0, process.stderr
  assert json.loads(process.stdout) == {'returncode': -signal.SIGKILL}


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'name',
  [
    'ptrace',
    'mount',
    'unshare',
    'io_uring_setup',
    'bpf',
    'memfd_create',
    'msgget',
    'semget',
    'shmget',
    'mq_open',
  ],
)
async def test_real_worker_denies_kernel_and_ipc_escape_syscalls(tmp_path, name):
  with pytest.raises(RuntimeError, match='SIGSYS'):
    await SubprocessRunner().run(job(tmp_path, 'probe', probe={'action': 'syscall', 'name': name}))


@pytest.mark.asyncio
async def test_real_worker_cpu_bound_is_named(tmp_path):
  with pytest.raises(RuntimeError, match='SIGXCPU.*CPU bound'):
    await SubprocessRunner(SandboxLimits(cpu_seconds=1)).run(
      job(tmp_path, 'probe', probe={'action': 'cpu'})
    )


@pytest.mark.asyncio
async def test_real_worker_keeps_backstops_and_allows_executable_anonymous_memory(tmp_path):

  runner = SubprocessRunner()
  result = await runner.run(job(tmp_path, 'probe', probe={'action': 'backstops'}))
  assert result.ok['value'] == {'death_signal': signal.SIGKILL, 'dumpable': 0, 'no_new_privs': 1}
  result = await runner.run(job(tmp_path, 'probe', probe={'action': 'io'}))
  assert result.ok['value']['memory'] == 'executable'


@pytest.mark.asyncio
async def test_real_worker_reports_missing_protection_as_unavailable(tmp_path):
  with pytest.raises(RuntimeError, match='file sandbox unavailable: missing landlock'):
    await SubprocessRunner().run(job(tmp_path, provider='missing_protection'))


@pytest.mark.asyncio
async def test_unnamed_realtime_signal_is_a_numbered_runtime_failure(tmp_path):
  number = signal.SIGRTMIN + 1
  with pytest.raises(RuntimeError, match=rf'signal {number}'):
    await SubprocessRunner().run(
      job(
        tmp_path,
        'probe',
        probe={'action': 'syscall', 'name': 'kill', 'arguments': ['@self', number]},
      )
    )
