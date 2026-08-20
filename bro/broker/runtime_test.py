import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass

import pytest

from bro.broker.brotocol import Message
from bro.broker.runtime import Peer, Runtime
from bro.broker.spawn import ChildHandle, LaunchSpec, Spawner
from bro.broker.transport import Provisioned, connect
from bro.broker.transports.unix import UnixServerTransport

TIMEOUT = 5.0
_RING_BYTES = 65536

# --- a lightweight non-Docker spawner: a `python -c` child over the real transport ----


@dataclass
class LocalLaunchSpec(LaunchSpec):
  code: str  # python source the child runs; it reaches back via BROKER_CHANNEL
  ring_bytes: int = _RING_BYTES


class LocalChildHandle(ChildHandle):
  def __init__(self, process: asyncio.subprocess.Process, ring_bytes: int):
    self._process = process
    self._ring_bytes = ring_bytes
    self._ring = bytearray()
    self._drain_task = asyncio.create_task(self._drain_output())

  async def _drain_output(self) -> None:
    assert self._process.stdout is not None  # carries stderr too (merged at spawn)
    while True:
      chunk = await self._process.stdout.read(4096)
      if len(chunk) == 0:
        return
      self._ring += chunk
      if len(self._ring) > self._ring_bytes:
        del self._ring[: -self._ring_bytes]

  async def wait(self) -> int:
    code = await self._process.wait()
    await self._drain_task  # output fully captured before the tail is read
    return code

  async def kill(self) -> None:
    if self._process.returncode is None:
      self._process.kill()
    await self._process.wait()
    await self._drain_task

  def output_tail(self) -> str:
    return bytes(self._ring).decode('utf-8', errors='replace')


class LocalSpawner(Spawner):
  def __init__(self):
    self.handles: list[LocalChildHandle] = []
    self.raise_on_spawn = False

  async def spawn(self, launch: LaunchSpec, channel: Provisioned) -> ChildHandle:
    assert isinstance(launch, LocalLaunchSpec)
    if self.raise_on_spawn:
      raise RuntimeError('injected launch failure')
    process = await asyncio.create_subprocess_exec(
      sys.executable,
      '-c',
      launch.code,
      env={**os.environ, 'BROKER_CHANNEL': 'unix:' + channel.host_endpoint},
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    handle = LocalChildHandle(process, launch.ring_bytes)
    self.handles.append(handle)
    return handle

  async def cleanup(self) -> None:
    await asyncio.gather(*(h.kill() for h in self.handles), return_exceptions=True)


class FakeListener:
  """captures every Runtime event onto one queue, preserving emission order."""

  def __init__(self):
    self.events: asyncio.Queue = asyncio.Queue()

  def on_connect(self, peer: Peer) -> None:
    self.events.put_nowait(('connect', peer))

  def on_message(self, peer: Peer, message: Message) -> None:
    self.events.put_nowait(('message', peer, message))

  def on_exit(self, peer: Peer, code: int, output: str) -> None:
    self.events.put_nowait(('exit', peer, code, output))

  def on_timeout(self, peer: Peer) -> None:
    self.events.put_nowait(('timeout', peer))

  def on_gone(self, peer: Peer) -> None:
    self.events.put_nowait(('gone', peer))


@dataclass
class Env:
  runtime: Runtime
  spawner: LocalSpawner
  listener: FakeListener
  control_dir: str


@contextlib.asynccontextmanager
async def runtime_harness(socket_dir):
  control_dir = str(socket_dir)
  transport = UnixServerTransport(control_dir)
  spawner = LocalSpawner()
  listener = FakeListener()
  runtime = Runtime(transport, spawner, listener)
  serve_task = asyncio.create_task(runtime.serve())
  await asyncio.sleep(0)  # let serve install the sink before any connection is accepted
  try:
    yield Env(runtime=runtime, spawner=spawner, listener=listener, control_dir=control_dir)
  finally:
    await runtime.stop()
    await asyncio.wait_for(serve_task, TIMEOUT)
    await spawner.cleanup()  # kill any child a forget/leak left running


async def next_event(listener: FakeListener):
  return await asyncio.wait_for(listener.events.get(), TIMEOUT)


async def until(predicate) -> None:
  """yield to the loop until |predicate| holds; TIMEOUT bounds only a genuine hang"""
  deadline = asyncio.get_running_loop().time() + TIMEOUT
  while not predicate():
    assert asyncio.get_running_loop().time() < deadline, 'condition not met within TIMEOUT'
    await asyncio.sleep(0)


def _sock_files(control_dir: str) -> list[str]:
  if not os.path.isdir(control_dir):
    return []
  return [name for name in os.listdir(control_dir) if name.endswith('.sock')]


_CHILD_COMPLETE = """
import os
from bro.broker.transport import connect
from bro.broker.brotocol import Message
client = connect(os.environ['BROKER_CHANNEL'])
client.send(Message(type='started', payload={'trail_id': 't1'}))
client.send(Message(type='completed', payload={'result': 'ok', 'end_reason': 'ok'}))
client.close()
"""

_CHILD_FAIL = """
import os, sys
from bro.broker.transport import connect
from bro.broker.brotocol import Message
client = connect(os.environ['BROKER_CHANNEL'])
client.send(Message(type='started', payload={}))
sys.stderr.write('boom-traceback')
sys.stderr.flush()
client.close()
sys.exit(3)
"""

_CHILD_HANG = """
import os, time
from bro.broker.transport import connect
from bro.broker.brotocol import Message
client = connect(os.environ['BROKER_CHANNEL'])
client.send(Message(type='started', payload={}))
time.sleep(3600)
"""

_CHILD_ECHO = """
import os
from bro.broker.transport import connect
from bro.broker.brotocol import Message
client = connect(os.environ['BROKER_CHANNEL'])
client.send(Message(type='started', payload={}))
request = client.receive(5.0)
payload = request.payload if request is not None else None
client.send(Message(type='completed', payload={'echo': payload, 'end_reason': 'ok'}))
client.close()
"""

_CHILD_EXIT_BEFORE_CONNECT = """
import sys
sys.exit(2)
"""


@pytest.mark.asyncio
async def test_connect_message_and_clean_exit_after_drain(socket_dir):
  # the canonical acceptance ordering: a completed the child writes just before exiting
  # is delivered as on_message *before* on_exit (drain-before-decide).
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_COMPLETE), timeout=None)

    assert await next_event(env.listener) == ('connect', peer)
    started = await next_event(env.listener)
    assert started[0] == 'message' and started[2].type == 'started'
    completed = await next_event(env.listener)
    assert completed[0] == 'message' and completed[2].type == 'completed'
    exited = await next_event(env.listener)
    assert exited == ('exit', peer, 0, '')


@pytest.mark.asyncio
async def test_early_exit_reports_code_and_output_tail(socket_dir):
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_FAIL), timeout=None)

    assert await next_event(env.listener) == ('connect', peer)
    assert (await next_event(env.listener))[0] == 'message'  # started
    kind, exited_peer, code, output = await next_event(env.listener)
    assert (kind, exited_peer, code) == ('exit', peer, 3)
    assert 'boom-traceback' in output


@pytest.mark.asyncio
async def test_timeout_kills_then_reports_timeout_and_exit(socket_dir):
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_HANG), timeout=3600.0)

    assert await next_event(env.listener) == ('connect', peer)
    assert (await next_event(env.listener))[0] == 'message'  # started
    # fire the timeout deterministically: the timer runs from spawn, so any real
    # duration races child startup; scheduling itself is call_later's contract,
    # what's ours to test is the fire path — kill, then the event sequence
    timer = env.runtime._peers[peer].timer
    assert timer is not None  # spawn wired the timeout
    timer.cancel()
    env.runtime._fire_timeout(peer)
    assert await next_event(env.listener) == ('timeout', peer)  # emitted after the kill
    kind, exited_peer, code, _ = await next_event(env.listener)
    assert (kind, exited_peer) == ('exit', peer)
    assert code != 0  # reaped after the kill signal


@pytest.mark.asyncio
async def test_send_delivers_message_to_peer(socket_dir):
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_ECHO), timeout=None)

    assert await next_event(env.listener) == ('connect', peer)
    assert (await next_event(env.listener))[0] == 'message'  # started
    env.runtime.send(peer, Message(type='ping', payload={'n': 7}))
    echoed = await next_event(env.listener)
    assert echoed[0] == 'message' and echoed[2].type == 'completed'
    assert echoed[2].payload['echo'] == {'n': 7}


@pytest.mark.asyncio
async def test_kill_reaps_the_process(socket_dir):
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_HANG), timeout=None)

    assert await next_event(env.listener) == ('connect', peer)
    assert (await next_event(env.listener))[0] == 'message'  # started
    env.runtime.kill(peer)
    kind, exited_peer, code, _ = await next_event(env.listener)
    assert (kind, exited_peer) == ('exit', peer)
    assert code != 0


@pytest.mark.asyncio
async def test_forget_drops_channel_without_exit(socket_dir):
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_HANG), timeout=None)

    assert await next_event(env.listener) == ('connect', peer)
    assert (await next_event(env.listener))[0] == 'message'  # started
    env.runtime.forget(peer)
    await until(lambda: _sock_files(env.control_dir) == [])  # the scheduled transport.close ran
    assert env.listener.events.empty()  # a forgotten peer's exit is not reported


@pytest.mark.asyncio
async def test_exit_before_connect_reports_without_birth(socket_dir):
  async with runtime_harness(socket_dir) as env:
    peer = await env.runtime.spawn(LocalLaunchSpec(_CHILD_EXIT_BEFORE_CONNECT), timeout=None)

    exited = await next_event(env.listener)  # no on_connect: the child never attached
    assert exited == ('exit', peer, 2, '')


@pytest.mark.asyncio
async def test_expect_delivers_messages_then_reports_gone_on_disconnect(socket_dir):
  # the expected-peer acceptance ordering: everything the peer wrote lands as
  # on_message before its EOF reports on_gone — the external analogue of
  # drain-before-decide, with no on_exit (there is no process to reap).
  async with runtime_harness(socket_dir) as env:
    provisioned = await env.runtime.expect(timeout=None)
    peer = provisioned.channel

    def _attach_and_complete() -> None:
      client = connect('unix:' + provisioned.host_endpoint)
      client.send(Message(type='completed', payload={'result': 'done', 'end_reason': 'ok'}))
      client.close()

    await asyncio.to_thread(_attach_and_complete)
    assert await next_event(env.listener) == ('connect', peer)
    completed = await next_event(env.listener)
    assert completed[0] == 'message' and completed[2].type == 'completed'
    assert await next_event(env.listener) == ('gone', peer)


@pytest.mark.asyncio
async def test_expect_disconnect_without_terminal_reports_gone(socket_dir):
  async with runtime_harness(socket_dir) as env:
    provisioned = await env.runtime.expect(timeout=None)
    peer = provisioned.channel

    def _attach_and_leave() -> None:
      client = connect('unix:' + provisioned.host_endpoint)
      client.send(Message(type='started', payload={'trail_id': 't1'}))
      client.close()

    await asyncio.to_thread(_attach_and_leave)
    assert await next_event(env.listener) == ('connect', peer)
    assert (await next_event(env.listener))[0] == 'message'  # started
    assert await next_event(env.listener) == ('gone', peer)


@pytest.mark.asyncio
async def test_expect_kill_closes_channel_and_reports_gone(socket_dir):
  # kill on an expected peer that never attached: the channel closes (socket
  # unlinked) and the wait task reports gone — no process was ever ours to kill.
  async with runtime_harness(socket_dir) as env:
    provisioned = await env.runtime.expect(timeout=None)
    peer = provisioned.channel
    assert _sock_files(env.control_dir) != []

    env.runtime.kill(peer)
    assert await next_event(env.listener) == ('gone', peer)
    await until(lambda: _sock_files(env.control_dir) == [])


@pytest.mark.asyncio
async def test_expect_timeout_reports_timeout_then_gone(socket_dir):
  async with runtime_harness(socket_dir) as env:
    provisioned = await env.runtime.expect(timeout=3600.0)
    peer = provisioned.channel
    # fire deterministically, as in the spawn timeout test
    timer = env.runtime._peers[peer].timer
    assert timer is not None
    timer.cancel()
    env.runtime._fire_timeout(peer)

    assert await next_event(env.listener) == ('timeout', peer)
    assert await next_event(env.listener) == ('gone', peer)


@pytest.mark.asyncio
async def test_launch_failure_rolls_back_registration(socket_dir):
  async with runtime_harness(socket_dir) as env:
    env.spawner.raise_on_spawn = True
    with pytest.raises(RuntimeError):
      await env.runtime.spawn(LocalLaunchSpec(_CHILD_COMPLETE), timeout=None)

    await until(lambda: _sock_files(env.control_dir) == [])  # the rollback's transport.close ran
    assert env.listener.events.empty()
