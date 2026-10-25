"""Bounded, event-loop-owned subprocess calls and strict reply validation."""

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Protocol, cast

from bro.files.confinement import SandboxLimits, SandboxReport, architecture, validate_report
from bro.files.models import validate_payload
from bro.files.protocol import (
  MAX_REPLY_BYTES,
  MAX_STDERR_BYTES,
  Job,
  Reply,
  decode_json,
  encode_frame,
)
from bro.files.text import validate_strings

logger = logging.getLogger(__name__)


class Runner(Protocol):
  async def run(self, job: Job) -> Reply: ...


def validate_reply(data: bytes, job: Job) -> Reply:
  try:
    if len(data) < 8:
      raise ValueError('no complete reply frame')
    size = int.from_bytes(data[:8], 'big')
    if size > MAX_REPLY_BYTES - 8 or len(data) != size + 8:
      raise ValueError('invalid frame length or trailing bytes')
    value = decode_json(data[8:])
    validate_strings(value)
    if not isinstance(value, dict):
      raise ValueError('reply is not an object')
    if value.keys() == {'error'}:
      error = value['error']
      if (
        not isinstance(error, dict)
        or error.keys() != {'kind', 'message'}
        or error['kind'] not in ('invalid_input', 'unavailable', 'engine')
        or type(error['message']) is not str
        or not error['message']
      ):
        raise ValueError('invalid error shape')
    else:
      if (
        value.keys() != {'ok', 'engine', 'sandbox'}
        or not isinstance(value['ok'], dict)
        or type(value['engine']) is not str
        or not value['engine']
      ):
        raise ValueError('invalid success shape')
      validate_report(value['sandbox'])
  except (ValueError, TypeError, RuntimeError, RecursionError, OverflowError):
    raise RuntimeError('invalid file reply: malformed frame, shape, strings or sandbox report')
  if 'error' in value:
    error = value['error']
    if error['kind'] == 'invalid_input':
      raise ValueError(error['message'])
    raise RuntimeError(error['message'])
  model = (
    job.file_type.details
    if job.operation == 'info'
    else job.file_type.get_view(job.arguments['view']).content
  )
  return Reply(
    ok=validate_payload(model, value['ok'], label=job.file_type.name),
    engine=value['engine'],
    sandbox=cast(SandboxReport, value['sandbox']),
  )


def _signal_failure(returncode: int, cpu_seconds: float, limits: SandboxLimits) -> str:
  number = -returncode
  try:
    name = signal.Signals(number).name
  except ValueError:
    name = f'signal {number}'
  causes: dict[int, str] = {
    signal.SIGABRT: 'engine abort, possibly out of memory',
    signal.SIGSEGV: 'engine crash',
    signal.SIGXCPU: 'CPU bound exceeded',
    signal.SIGSYS: 'sandbox violation',
    signal.SIGALRM: 'wall clock exceeded (worker backstop)',
    signal.SIGKILL: (
      'CPU hard bound exceeded'
      if cpu_seconds >= limits.cpu_hard_seconds
      else 'possibly out of memory'
    ),
  }
  return f'file worker killed by {name}: {causes.get(number, "engine signal")}'


def _kill_group(process: subprocess.Popen[bytes]) -> None:
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass


async def _wait_for_exit(pidfd: int) -> bool:
  loop = asyncio.get_running_loop()
  exited = loop.create_future()
  interrupted = False

  def finish() -> None:
    if not exited.done():
      exited.set_result(None)

  with ExitStack() as stack:
    loop.add_reader(pidfd, finish)
    stack.callback(loop.remove_reader, pidfd)
    while not exited.done():
      try:
        await asyncio.shield(exited)
      except asyncio.CancelledError:
        interrupted = True
  return interrupted


@dataclass
class _Worker:
  process: subprocess.Popen[bytes]
  pidfd: int | None = None
  cpu_seconds: float = 0


class SubprocessRunner:
  def __init__(self, limits: SandboxLimits | None = None):
    self.limits = limits or SandboxLimits()
    self._workers: set[subprocess.Popen[bytes]] = set()
    self._closed = False

  def close(self) -> None:
    self._closed = True
    for process in self._workers:
      _kill_group(process)

  @asynccontextmanager
  async def _worker(self, job: Job, stdout: bytearray, stderr: bytearray) -> AsyncIterator[_Worker]:
    if self._closed:
      raise RuntimeError('file runner is closed')
    command = [
      sys.executable,
      '-I',
      '-B',
      '-X',
      'utf8',
      '-m',
      'bro.files.worker',
      job.file_type.provider,
      str(os.getpid()),
      json.dumps(asdict(self.limits)),
      json.dumps(asdict(job.file_type.limits)),
    ]
    process = subprocess.Popen(
      command,
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      env={},
      cwd='/',
      close_fds=True,
      start_new_session=True,
    )
    self._workers.add(process)
    worker = _Worker(process)
    with ExitStack() as stack:
      assert process.stdin is not None and process.stdout is not None and process.stderr is not None
      for stream in (process.stdin, process.stdout, process.stderr):
        stack.enter_context(stream)
      try:
        for stream in (process.stdin, process.stdout, process.stderr):
          os.set_blocking(stream.fileno(), False)
        worker.pidfd = os.pidfd_open(process.pid)
        stack.callback(os.close, worker.pidfd)
        yield worker
      finally:
        pending_error = sys.exception()
        _kill_group(process)
        interrupted = await _wait_for_exit(worker.pidfd) if worker.pidfd is not None else False
        # Keep the leader unreaped until the group has been killed, preventing pid reuse.
        _, status, usage = os.wait4(process.pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
        worker.cpu_seconds = usage.ru_utime + usage.ru_stime
        self._workers.remove(process)
        try:
          self._drain(process, stdout, stderr)
        except RuntimeError:
          if pending_error is None and not interrupted:
            raise
        if interrupted:
          raise asyncio.CancelledError

  async def run(self, job: Job) -> Reply:
    started = time.monotonic()
    stdout, stderr = bytearray(), bytearray()
    engine = None
    outcome = 'cancelled'
    try:
      architecture()
      if len(job.snapshot) > job.file_type.limits.max_bytes:
        raise ValueError('file exceeds max_bytes; use a smaller file')
      async with self._worker(job, stdout, stderr) as worker:
        process = worker.process
        try:
          async with asyncio.timeout(self.limits.wall_seconds - (time.monotonic() - started)):
            await self._exchange(worker, job, stdout, stderr)
        except TimeoutError:
          raise RuntimeError('file worker wall clock exceeded')
      assert process.returncode is not None
      if process.returncode < 0:
        raise RuntimeError(_signal_failure(process.returncode, worker.cpu_seconds, self.limits))
      if process.returncode != 0:
        raise RuntimeError(f'file worker exited with status {process.returncode}')
      reply = validate_reply(bytes(stdout), job)
      engine = reply.engine
      outcome = 'ok'
      return reply
    except OSError as error:
      outcome = f'file sandbox unavailable: cannot start or supervise worker: {error}'
      raise RuntimeError(outcome) from error
    except Exception as error:
      outcome = str(error)
      raise
    finally:
      logger.info(
        'file operation=%s type=%s sha256=%s engine=%s duration=%.3f outcome=%s stderr=%r',
        job.operation,
        job.file_type.name,
        hashlib.sha256(job.snapshot).hexdigest(),
        engine,
        time.monotonic() - started,
        outcome,
        bytes(stderr[:4096]) if outcome != 'ok' else b'',
      )

  def _read(self, descriptor: int, output: bytearray, maximum: int, label: str) -> bool:
    try:
      chunk = os.read(descriptor, min(65536, maximum + 1 - len(output)))
    except BlockingIOError:
      return True
    output.extend(chunk)
    if len(output) > maximum:
      raise RuntimeError(f'file worker {label} exceeds its byte limit')
    if label == 'reply' and len(output) >= 8 and int.from_bytes(output[:8], 'big') > maximum - 8:
      raise RuntimeError('file worker reply exceeds its byte limit')
    return bool(chunk)

  def _drain(self, process: subprocess.Popen[bytes], stdout: bytearray, stderr: bytearray) -> None:
    failure = None
    for stream, output, maximum, label in (
      (process.stdout, stdout, MAX_REPLY_BYTES, 'reply'),
      (process.stderr, stderr, MAX_STDERR_BYTES, 'stderr'),
    ):
      assert stream is not None
      try:
        while True:
          before = len(output)
          if not self._read(stream.fileno(), output, maximum, label) or len(output) == before:
            break
      except RuntimeError as error:
        failure = failure or error
    if failure is not None:
      raise failure

  async def _exchange(
    self, worker: _Worker, job: Job, stdout: bytearray, stderr: bytearray
  ) -> None:
    process = worker.process
    assert worker.pidfd is not None
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    input_stream = process.stdin
    loop = asyncio.get_running_loop()
    request = encode_frame(
      {
        'operation': job.operation,
        'arguments': job.arguments,
        'snapshot_size_bytes': len(job.snapshot),
      }
    )
    chunks = [memoryview(request), memoryview(job.snapshot)]
    ready: set[int] = set()
    changed = asyncio.Event()
    descriptors = [process.stdin.fileno(), process.stdout.fileno(), process.stderr.fileno()]
    with ExitStack() as stack:
      pidfd = worker.pidfd

      def wake(descriptor: int) -> None:
        ready.add(descriptor)
        changed.set()

      for descriptor in [*descriptors[1:], pidfd]:
        loop.add_reader(descriptor, wake, descriptor)
        stack.callback(loop.remove_reader, descriptor)
      loop.add_writer(descriptors[0], wake, descriptors[0])
      stack.callback(lambda: None if input_stream.closed else loop.remove_writer(descriptors[0]))
      while True:
        await changed.wait()
        changed.clear()
        events, ready = ready, set()
        for descriptor, output, maximum, label in (
          (descriptors[1], stdout, MAX_REPLY_BYTES, 'reply'),
          (descriptors[2], stderr, MAX_STDERR_BYTES, 'stderr'),
        ):
          if descriptor in events and not self._read(descriptor, output, maximum, label):
            loop.remove_reader(descriptor)
        if pidfd in events:
          return
        if descriptors[0] in events:
          while chunks and not chunks[0]:
            chunks.pop(0)
          if not chunks:
            loop.remove_writer(descriptors[0])
            process.stdin.close()
          else:
            try:
              written = os.write(descriptors[0], chunks[0][:65536])
              chunks[0] = chunks[0][written:]
            except BrokenPipeError:
              loop.remove_writer(descriptors[0])
              process.stdin.close()
            except BlockingIOError:
              pass
