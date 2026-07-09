import asyncio
from typing import Optional

import pytest

from broker.brotocol import Message, Tag
from broker.dispatcher import DEFAULT_TIMEOUT, Dispatcher, ping_handler, spawn_test_handler
from broker.runtime import Peer
from broker.spawn import LaunchSpec

_LAUNCH = LaunchSpec()  # opaque marker; the fake Runtime never inspects it


class FakeRuntime:
  """records the commands the Dispatcher issues and hands it test-chosen peer ids.

  Structurally a `broker.dispatcher.RuntimeCommands`; the Dispatcher drives it while the
  test drives the Dispatcher's listener callbacks, so the rules are exercised with no loop,
  socket, or subprocess.
  """

  def __init__(self):
    self.sent: list[tuple[Peer, Message]] = []
    self.spawns: list[tuple[LaunchSpec, Optional[float]]] = []
    self.forgotten: list[Peer] = []
    self.killed: list[Peer] = []
    self.next_peers: list[Peer] = []  # spawn returns these front-to-back
    self.spawn_error: Optional[BaseException] = None
    self._stopped = asyncio.Event()

  async def spawn(self, launch: LaunchSpec, *, timeout: Optional[float]) -> Peer:
    self.spawns.append((launch, timeout))
    if self.spawn_error is not None:
      raise self.spawn_error
    return self.next_peers.pop(0)

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


def make_dispatcher() -> tuple[Dispatcher, FakeRuntime]:
  runtime = FakeRuntime()
  dispatcher = Dispatcher()
  dispatcher.bind(runtime)
  return dispatcher, runtime


async def _settle() -> None:
  # let a scheduled Runtime.spawn task and its topology-registration done-callback run.
  for _ in range(4):
    await asyncio.sleep(0)


async def spawn_child(
  dispatcher: Dispatcher, runtime: FakeRuntime, *, parent: Peer = 'parent', request_id: str = 'R'
) -> Peer:
  """drive a spawn request through rule 3 + the spawn-test handler; return the child peer."""
  child = 'child'
  runtime.next_peers.append(child)
  dispatcher.on(Tag.SPAWN, spawn_test_handler(_LAUNCH))
  dispatcher.on_message(parent, Message(type=Tag.SPAWN, id=request_id, payload={}))
  await _settle()
  return child


@pytest.mark.asyncio
async def test_ping_replies_correlated():
  # rule 3: a fresh typed request invokes its handler, which replies via Tag.REPLY.
  dispatcher, runtime = make_dispatcher()
  dispatcher.on(Tag.PING, ping_handler)
  dispatcher.on_message('caller', Message(type=Tag.PING, id='Q', payload={'n': 7}))
  assert len(runtime.sent) == 1
  target, reply = runtime.sent[0]
  assert target == 'caller'
  assert reply.type == Tag.REPLY
  assert reply.in_reply_to == 'Q'
  assert reply.payload == {'pong': {'n': 7}}


@pytest.mark.asyncio
async def test_spawn_registers_topology_and_default_timeout():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  assert runtime.spawns == [(_LAUNCH, DEFAULT_TIMEOUT)]
  assert dispatcher.origin[child] == ('parent', 'R')
  assert dispatcher.pending['R'] == 'parent'
  assert dispatcher.parent[child] == 'parent'
  assert child in dispatcher.children['parent']


@pytest.mark.asyncio
async def test_child_lifecycle_routes_to_parent_retagged():
  # rule 2: a spawned peer's started/completed go to its parent, re-tagged to the origin request.
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(child, Message(type=Tag.STARTED, payload={'trail_id': 't'}))
  dispatcher.on_message(
    child, Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'terminal'})
  )
  started_target, started = runtime.sent[-2]
  completed_target, completed = runtime.sent[-1]
  assert (started_target, started.type, started.in_reply_to) == ('parent', Tag.STARTED, 'R')
  assert (completed_target, completed.type, completed.in_reply_to) == ('parent', Tag.COMPLETED, 'R')
  assert child in dispatcher.finalized  # completed is terminal


@pytest.mark.asyncio
async def test_drop_after_terminal():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(
    child, Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'terminal'})
  )
  delivered = len(runtime.sent)
  dispatcher.on_message(child, Message(type=Tag.STARTED, payload={}))  # after terminal
  assert len(runtime.sent) == delivered  # dropped, not routed


@pytest.mark.asyncio
async def test_completed_then_exit_is_a_single_terminal():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(
    child, Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'terminal'})
  )
  dispatcher.on_exit(child, 0, '')  # finalized already -> no synthesized failed
  assert [m.type for _, m in runtime.sent].count(Tag.FAILED) == 0
  assert runtime.forgotten == [child]
  assert child not in dispatcher.origin  # cleaned up
  assert child not in dispatcher.finalized


@pytest.mark.asyncio
async def test_exit_without_completed_synthesizes_failed_exit():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_exit(child, 3, 'boom-traceback')
  target, failed = runtime.sent[-1]
  assert target == 'parent'
  assert failed.type == Tag.FAILED
  assert failed.in_reply_to == 'R'
  assert failed.payload == {'reason': 'exit', 'exit_code': 3, 'output_tail': 'boom-traceback'}
  assert runtime.forgotten == [child]


@pytest.mark.asyncio
async def test_timeout_synthesizes_failed_and_the_later_exit_dedupes():
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_timeout(child)
  target, failed = runtime.sent[-1]
  assert (target, failed.type, failed.payload, failed.in_reply_to) == (
    'parent',
    Tag.FAILED,
    {'reason': 'timeout'},
    'R',
  )
  dispatcher.on_exit(child, -9, '')  # the Runtime-killed peer's reap, already finalized
  assert [m.type for _, m in runtime.sent].count(Tag.FAILED) == 1
  assert runtime.forgotten == [child]


@pytest.mark.asyncio
async def test_spawn_failure_synthesizes_failed_launch():
  # a launch that raises feeds back as failed{reason: 'launch'} correlated to the origin
  # request instead of leaving the requester to hang to its timeout.
  dispatcher, runtime = make_dispatcher()
  runtime.spawn_error = RuntimeError('image build exploded')
  dispatcher.on(Tag.SPAWN, spawn_test_handler(_LAUNCH))
  dispatcher.on_message('parent', Message(type=Tag.SPAWN, id='R', payload={}))
  await _settle()
  assert len(runtime.sent) == 1
  target, failed = runtime.sent[0]
  assert target == 'parent'
  assert failed.type == Tag.FAILED
  assert failed.in_reply_to == 'R'
  assert failed.payload == {'reason': 'launch', 'error': 'image build exploded'}
  assert dispatcher.origin == {}  # no topology was registered for the never-launched child
  assert dispatcher.pending == {}


@pytest.mark.asyncio
async def test_reply_to_an_awaited_request_is_delivered_as_is():
  # rule 1: a message whose in_reply_to a peer awaits goes to that requester, unchanged.
  dispatcher, runtime = make_dispatcher()
  child = await spawn_child(dispatcher, runtime)
  reply = Message(type=Tag.REPLY, in_reply_to='R', payload={'ok': 1})
  dispatcher.on_message(child, reply)
  assert runtime.sent[-1] == ('parent', reply)
  assert child not in dispatcher.finalized  # a non-completed reply is not terminal


@pytest.mark.asyncio
async def test_unroutable_messages_are_refused():
  # rule 4: no handler / no origin / nobody awaiting -> dropped, nothing delivered.
  dispatcher, runtime = make_dispatcher()
  dispatcher.on_message('stranger', Message(type='mystery', payload={}))
  dispatcher.on_message('stranger', Message(type=Tag.STARTED, payload={}))
  dispatcher.on_message('stranger', Message(type=Tag.REPLY, in_reply_to='nobody', payload={}))
  assert runtime.sent == []


@pytest.mark.asyncio
async def test_root_lifecycle_is_dropped_without_refusal_or_finalizing(caplog):
  # the root has no parent to notify: its started/completed are dropped (no refusal
  # warning) and its completed is not a terminal — the channel keeps serving the session.
  dispatcher, runtime = make_dispatcher()
  runtime.next_peers.append('root')
  run_task = asyncio.ensure_future(dispatcher.run(_LAUNCH))
  await _settle()
  dispatcher.on_message('root', Message(type=Tag.STARTED, payload={'trail_id': 't'}))
  dispatcher.on_message(
    'root', Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'terminal'})
  )
  assert runtime.sent == []
  assert not any('refused' in record.message for record in caplog.records)
  assert 'root' not in dispatcher.finalized
  dispatcher.on(Tag.PING, ping_handler)
  dispatcher.on_message('root', Message(type=Tag.PING, id='Q', payload={}))
  assert runtime.sent[-1][1].type == Tag.REPLY  # not drop-gated: requests still served
  dispatcher.stop()
  await asyncio.wait_for(run_task, 5)


@pytest.mark.asyncio
async def test_root_lifecycle_invokes_registered_handlers_but_child_lifecycle_still_routes():
  # rule 3 precedes the drop: a consumer can mount started/completed handlers to surface
  # the root's lifecycle. A spawned child's lifecycle keeps rule-2 routing to its parent.
  dispatcher, runtime = make_dispatcher()
  handled: list[tuple[Peer, str]] = []
  dispatcher.on(Tag.STARTED, lambda context, peer, message: handled.append((peer, message.type)))
  dispatcher.on(Tag.COMPLETED, lambda context, peer, message: handled.append((peer, message.type)))
  runtime.next_peers.append('root')
  run_task = asyncio.ensure_future(dispatcher.run(_LAUNCH))
  await _settle()
  dispatcher.on_message('root', Message(type=Tag.STARTED, payload={'trail_id': 't'}))
  dispatcher.on_message(
    'root', Message(type=Tag.COMPLETED, payload={'result': 'ok', 'end_reason': 'terminal'})
  )
  assert handled == [('root', Tag.STARTED), ('root', Tag.COMPLETED)]
  assert 'root' not in dispatcher.finalized
  child = await spawn_child(dispatcher, runtime, parent='root')
  dispatcher.on_message(child, Message(type=Tag.STARTED, payload={}))
  assert handled == [('root', Tag.STARTED), ('root', Tag.COMPLETED)]
  assert runtime.sent[-1][0] == 'root'  # the child's started routed to its parent
  dispatcher.stop()
  await asyncio.wait_for(run_task, 5)


def make_tap(dispatcher: Dispatcher) -> list[tuple[Optional[Peer], Peer, Message]]:
  observed: list[tuple[Optional[Peer], Peer, Message]] = []
  dispatcher.add_delivery_observer(
    lambda source, target, message: observed.append((source, target, message))
  )
  return observed


@pytest.mark.asyncio
async def test_delivery_tap_observes_rule_1_and_2_deliveries():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_message(child, Message(type=Tag.STARTED, payload={'trail_id': 't'}))  # rule 2
  dispatcher.on_message(
    child, Message(type=Tag.REPLY, in_reply_to='R', payload={'ok': 1})
  )  # rule 1
  assert [(source, target, message.type) for source, target, message in observed] == [
    (child, 'parent', Tag.STARTED),
    (child, 'parent', Tag.REPLY),
  ]
  assert observed[0][2].in_reply_to == 'R'  # the tap sees the delivered (re-tagged) message


@pytest.mark.asyncio
async def test_delivery_tap_observes_synthesized_failed():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  child = await spawn_child(dispatcher, runtime)
  dispatcher.on_exit(child, 3, 'tail')
  [(source, target, failed)] = observed
  assert (source, target, failed.type) == (child, 'parent', Tag.FAILED)
  assert failed.payload['reason'] == 'exit'


@pytest.mark.asyncio
async def test_delivery_tap_observes_launch_failure_with_no_source_peer():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  runtime.spawn_error = RuntimeError('boom')
  dispatcher.on(Tag.SPAWN, spawn_test_handler(_LAUNCH))
  dispatcher.on_message('parent', Message(type=Tag.SPAWN, id='R', payload={}))
  await _settle()
  [(source, target, failed)] = observed
  assert source is None  # the child never existed
  assert (target, failed.type, failed.payload['reason']) == ('parent', Tag.FAILED, 'launch')


@pytest.mark.asyncio
async def test_delivery_tap_ignores_handler_replies_and_refusals():
  dispatcher, runtime = make_dispatcher()
  observed = make_tap(dispatcher)
  dispatcher.on(Tag.PING, ping_handler)
  dispatcher.on_message('caller', Message(type=Tag.PING, id='Q', payload={}))  # rule 3 + reply()
  dispatcher.on_message('stranger', Message(type='mystery', payload={}))  # rule 4
  assert observed == []
  assert len(runtime.sent) == 1  # the ping reply itself was still delivered


@pytest.mark.asyncio
async def test_run_spawns_root_uniformly_and_returns_its_exit_code():
  dispatcher, runtime = make_dispatcher()
  runtime.next_peers.append('root')
  assert dispatcher.root is None  # unset until run() spawns it
  run_task = asyncio.ensure_future(dispatcher.run(_LAUNCH))
  await _settle()
  assert runtime.spawns == [(_LAUNCH, None)]  # the root carries no request-lifecycle timeout
  assert dispatcher.root == 'root'
  dispatcher.on_exit('root', 7, '')
  assert await asyncio.wait_for(run_task, 5) == 7
  assert all(m.type != Tag.FAILED for _, m in runtime.sent)  # root has no origin -> no synthesis
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
