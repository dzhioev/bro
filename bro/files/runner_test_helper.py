"""Adversarial providers imported by the isolated worker."""

import ctypes
import errno
import json
import os
import signal
import sys
from typing import Any

from bro.files.confinement_probe_test_helper import attempt
from bro.files.protocol import InvalidInput, encode_frame
from bro.files.types import Limits


class TestProvider:
  def prepare(self) -> str:
    self.environment = dict(os.environ)
    self.descriptors = [descriptor for descriptor in range(64) if self._exists(descriptor)]
    return 'test-engine 1'

  @staticmethod
  def _exists(descriptor: int) -> bool:
    try:
      os.fstat(descriptor)
    except OSError:
      return False
    return True

  def info(self, snapshot: bytes, limits: Limits) -> dict[str, Any]:
    request = json.loads(snapshot)
    action = request['action']
    if action == 'invalid':
      raise InvalidInput('document has no pages; use a document with pages')
    if action == 'engine':
      raise LookupError('private document content')
    if action == 'segfault':
      ctypes.string_at(0)
    if action == 'abort':
      os.abort()
    if action == 'hold':
      os.write(2, b'worker ready\n')
      signal.pause()
    if action == 'stderr':
      os.write(2, b'x' * (128 * 1024))
    if action == 'stdout':
      os.write(1, b'x' * (128 * 1024))
    if action == 'print':
      os.write(1, b'engine diagnostics\n')
    if action == 'probe':
      try:
        result = attempt(request['probe'])
      except OSError as error:
        result = {'errno': error.errno}
      return {'value': result}
    if action == 'environment':
      return {'value': {'environment': self.environment, 'descriptors': self.descriptors}}
    if action == 'reply_flood':
      descriptor = next(descriptor for descriptor in range(3, 64) if self._exists(descriptor))
      os.write(descriptor, (8 * 1024**2 - 8).to_bytes(8, 'big') + b'x' * (9 * 1024**2))
    if action == 'raw':
      descriptor = next(descriptor for descriptor in range(3, 64) if self._exists(descriptor))
      os.write(descriptor, bytes.fromhex(request['hex']))
      if request.get('signal'):
        os.abort()
      os._exit(0)
    if action == 'bad_model':
      return {'wrong': 'shape'}
    if action == 'bad_string':
      return {'value': '\x00'}
    return {'value': request.get('value', 'Здравствуйте')}

  def view(
    self, snapshot: bytes, view: str, pages: list[int] | None, limits: Limits
  ) -> dict[str, Any]:
    return {'value': {'view': view, 'pages': pages, 'size': len(snapshot)}}


provider = TestProvider()


class EarlyExit(TestProvider):
  def prepare(self) -> str:
    os.write(1, encode_frame({'error': {'kind': 'invalid_input', 'message': 'early refusal'}}))
    os._exit(0)


early_exit = EarlyExit()


class ConcurrentOutput(TestProvider):
  def prepare(self) -> str:
    os.write(2, b'preparation diagnostics\n' * 1000)
    return super().prepare()


concurrent_output = ConcurrentOutput()


class GroupProvider(TestProvider):
  def prepare(self) -> str:
    descendant = os.fork()
    if descendant == 0:
      signal.pause()
      os._exit(0)
    os.write(2, f'descendant {descendant}\n'.encode())
    return super().prepare()


group_provider = GroupProvider()


def parent_death_launcher() -> None:
  import subprocess
  from dataclasses import asdict

  from bro.files.confinement import SandboxLimits

  limits = Limits(1000, 5000, 100, 20)
  with subprocess.Popen(
    [
      sys.executable,
      '-I',
      '-B',
      '-X',
      'utf8',
      '-m',
      'bro.files.worker',
      'bro.files.runner_test_helper:provider',
      str(os.getpid()),
      json.dumps(asdict(SandboxLimits())),
      json.dumps(asdict(limits)),
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env={},
    cwd='/',
    start_new_session=True,
    close_fds=True,
  ) as worker:
    assert worker.stdin is not None and worker.stderr is not None
    snapshot = b'{"action":"hold"}'
    worker.stdin.write(
      encode_frame(
        {
          'operation': 'info',
          'arguments': {},
          'snapshot_size_bytes': len(snapshot),
        }
      )
      + snapshot
    )
    worker.stdin.flush()
    assert worker.stderr.readline() == b'worker ready\n'
    print(worker.pid, flush=True)
    # Parent death is the subject; bypass the context manager's normal reaping.
    os._exit(0)


def lifecycle_probe(mode: str) -> None:
  import asyncio
  import subprocess
  import tempfile
  from pathlib import Path
  from typing import Any

  from pydantic import BaseModel

  from bro.files.confinement import linux
  from bro.files.protocol import Job
  from bro.files.runner import SubprocessRunner
  from bro.files.types_test_helper import make_type

  linux.prctl(36, 1, 'test subreaper')
  if mode == 'parent_death':
    launcher = subprocess.run(
      [sys.executable, '-m', 'bro.files.runner_test_helper', 'launcher'],
      capture_output=True,
      check=True,
      timeout=10,
    )
    pid, status = os.waitpid(int(launcher.stdout), 0)
    print(json.dumps({'returncode': os.waitstatus_to_exitcode(status)}), flush=True)
    return

  class Payload(BaseModel):
    value: Any

  class ObservedRunner(SubprocessRunner):
    def __init__(self):
      super().__init__()
      self.ready = asyncio.Event()
      self.descendant = None

    def _read(self, descriptor, output, maximum, label):
      result = super()._read(descriptor, output, maximum, label)
      if label == 'stderr':
        for line in output.splitlines():
          if line.startswith(b'descendant '):
            self.descendant = int(line.split()[1])
        if b'worker ready\n' in output:
          self.ready.set()
      return result

  async def run(directory):
    runner = ObservedRunner()
    file_type = make_type(
      Path(directory), details=Payload, provider='bro.files.runner_test_helper:group_provider'
    )
    action = 'success' if mode == 'exit_with_open_pipes' else 'hold'
    task = asyncio.create_task(
      runner.run(Job(file_type, 'info', {}, json.dumps({'action': action}).encode()))
    )
    if mode == 'cancel_group':
      async with asyncio.timeout(10):
        await runner.ready.wait()
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass
    else:
      await task
    assert runner.descendant is not None
    _, status = os.waitpid(runner.descendant, 0)
    return {'returncode': os.waitstatus_to_exitcode(status)}

  with tempfile.TemporaryDirectory() as directory:
    print(json.dumps(asyncio.run(run(directory))), flush=True)


def http_disconnect_probe() -> None:
  import asyncio
  import socket
  import tempfile
  from contextlib import asynccontextmanager, closing
  from pathlib import Path
  from unittest.mock import patch

  import pytest
  import uvicorn
  from pydantic import BaseModel

  from bro.files import mcp, operations
  from bro.files.confinement import linux
  from bro.files.runner import SubprocessRunner
  from bro.files.slots import WorkerSlots
  from bro.files.types_test_helper import make_type
  from bro.llm.mcp import InProcessMCPServer
  from bro.runtime.mcp_server import create_http_app

  linux.prctl(36, 1, 'test subreaper')

  class Payload(BaseModel):
    value: Any

  class ObservedRunner(SubprocessRunner):
    def __init__(self):
      super().__init__()
      self.ready = asyncio.Event()
      self.process = None
      self.descendant = None

    async def _exchange(self, worker, job, stdout, stderr):
      self.process = worker.process
      await super()._exchange(worker, job, stdout, stderr)

    def _read(self, descriptor, output, maximum, label):
      result = super()._read(descriptor, output, maximum, label)
      if label == 'stderr':
        for line in output.splitlines():
          if line.startswith(b'descendant '):
            self.descendant = int(line.split()[1])
        if b'worker ready\n' in output:
          self.ready.set()
      return result

  @asynccontextmanager
  async def serving(app):
    ready = asyncio.Event()

    class Server(uvicorn.Server):
      async def startup(self, sockets=None):
        await super().startup(sockets)
        assert self.started
        ready.set()

    server = Server(uvicorn.Config(app, log_level='warning', lifespan='on'))
    with socket.create_server(('127.0.0.1', 0)) as listener:
      async with asyncio.TaskGroup() as tasks:
        tasks.create_task(server.serve(sockets=[listener]))
        try:
          await ready.wait()
          yield listener.getsockname()[1]
        finally:
          server.should_exit = True

  @asynccontextmanager
  async def request(port, path):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
      body = json.dumps(
        {
          'jsonrpc': '2.0',
          'id': 1,
          'method': 'tools/call',
          'params': {'name': 'info', 'arguments': {'path': str(path), 'type': 'sample'}},
        }
      ).encode()
      writer.write(
        b'POST /file HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer test-token\r\n'
        b'Accept: application/json, text/event-stream\r\nContent-Type: application/json\r\n'
        b'Connection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
      )
      await writer.drain()
      yield reader
    finally:
      writer.close()
      await writer.wait_closed()

  async def check(app, slots, runner, path):
    async with serving(app) as port:
      with closing(runner):
        async with asyncio.timeout(10), request(port, path):
          await runner.ready.wait()
          assert runner.process is not None and runner.descendant is not None
          assert os.getpgid(runner.descendant) == runner.process.pid
        process = runner.process
        async with asyncio.timeout(2):
          with await slots.acquire():
            assert process.returncode == -signal.SIGKILL
            _, status = await asyncio.to_thread(os.waitpid, runner.descendant, 0)
            assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
            with pytest.raises(ChildProcessError):
              os.waitpid(runner.process.pid, os.WNOHANG)
            try:
              os.killpg(runner.process.pid, 0)
            except ProcessLookupError:
              pass
            else:
              raise AssertionError('worker process group survived disconnect')
          path.write_bytes(b'{"action":"success"}')
          async with request(port, path) as reader:
            response = await reader.read()
          headers, _, body = response.partition(b'\r\n\r\n')
          assert headers.startswith(b'HTTP/1.1 200 '), response
          result = json.loads(body)['result']
          assert not result.get('isError', False), result
          assert result['structuredContent']['details']['value'] == 'Здравствуйте'
          _, status = await asyncio.to_thread(os.waitpid, runner.descendant, 0)
          assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    return {'returncode': process.returncode}

  with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'document'
    path.write_bytes(b'{"action":"hold"}')
    runner = ObservedRunner()
    slots = WorkerSlots(budget_bytes=runner.limits.address_space_bytes)
    file_type = make_type(
      Path(directory), details=Payload, provider='bro.files.runner_test_helper:group_provider'
    )
    with (
      closing(operations.Operations(runner, slots=slots)) as state,
      patch.object(operations.registry, 'get', return_value=file_type),
    ):
      server = InProcessMCPServer('file', mcp.toolset.tools(state), close=state.close)
      with closing(create_http_app([server], 'test-token')) as app:
        result = asyncio.run(check(app, slots, runner, path))
    print(json.dumps(result), flush=True)


class MissingProtection(TestProvider):
  def prepare(self) -> str:
    from bro.files.confinement import linux
    from bro.files.confinement.tables import TABLES

    arch = os.uname().machine
    linux.prctl(linux.PR_SET_NO_NEW_PRIVS, 1, 'no_new_privs')
    linux.install_filter(
      arch,
      [
        linux.Instruction(linux.LOAD, 0, 0, 0),
        linux.Instruction(linux.EQUAL, 0, 1, TABLES[arch]['landlock_create_ruleset']),
        linux.Instruction(linux.RETURN, 0, 0, linux.SECCOMP_RET_ERRNO | errno.ENOSYS),
        linux.Instruction(linux.RETURN, 0, 0, linux.SECCOMP_RET_ALLOW),
      ],
    )
    return super().prepare()


missing_protection = MissingProtection()


if __name__ == '__main__':
  if sys.argv[1] == 'launcher':
    parent_death_launcher()
  elif sys.argv[1] == 'http_disconnect':
    http_disconnect_probe()
  else:
    lifecycle_probe(sys.argv[1])
