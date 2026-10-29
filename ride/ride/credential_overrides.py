"""Pending credential overrides, supervisor acknowledgements and durable receipts."""

import asyncio
import base64
import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from bro.authority.records import dump_authority, dump_request
from bro.authority.rules import Mission
from bro.base import credentials, log
from bro.base.credential_apply import atomic_file
from bro.base.lulid import lulid
from bro.broker.brotocol import Message
from bro.broker.dispatcher import Dispatcher
from bro.broker.journal import Event, Journal, Record
from bro.broker.runtime import Peer
from bro.broker.transports.tcp import LOCAL_HOST
from bro.monitor import party_member_dir
from bro.worker_types import UnattributablePeer
from bro.workspace.paths import workspace_dir
from ride.authority_launch import hydrate, mission_view
from ride.authority_reading import Reading
from ride.overrides import apply as apply_authority, atomic_record, candidate, changes
from ride.peer_facts import PeerFacts, WorkerFacts
from ride.workspace.store import ScopedSecrets

ACK_TIMEOUT = 10.0
LIFETIME_READY = b'1'


def records_directory(facts: WorkerFacts) -> Path:
  if facts.workspace is None:
    raise ValueError('override check: mission has no workspace')
  path = workspace_dir(facts.workspace)
  return path if facts.member is None else party_member_dir(path, facts.member)


def receipt_path(directory: Path, operation: str) -> Path:
  return directory / 'credential-receipts' / (operation + '.json')


def receipt(directory: Path, operation: str) -> dict[str, Any] | None:
  try:
    value = json.loads(receipt_path(directory, operation).read_text())
  except FileNotFoundError:
    return None
  if (
    value.get('operation') != operation
    or type(value.get('generation')) is not int
    or value['generation'] < 1
  ):
    raise ValueError(f'invalid credential receipt for {operation}')
  return value


def settle_resume(directory: Path) -> None:
  path = directory / 'resume.json'
  if not path.exists():
    return
  data = json.loads(path.read_text())
  operation = data.get('override_operation', {})
  if operation.get('kind') != 'credentials' or operation.get('state') != 'pending':
    return
  acknowledged = receipt(directory, operation['id'])
  if acknowledged is not None:
    data.update(operation['values'])
  data['override_operation'] = {**operation, 'state': 'applied' if acknowledged else 'dropped'}
  atomic_record(path, data)


@dataclass
class ManualApplier:
  lifeline: Path
  stream: BinaryIO
  alive: bool = False
  claimed: bool = False


class CredentialOverrides:
  def __init__(self, peers: PeerFacts, journal: Journal, reading: Reading):
    self.peers, self.journal, self.reading = peers, journal, reading
    self._pending: dict[str, tuple[Mission, dict[str, Any], dict[str, Any]]] = {}
    self._signals: dict[str, asyncio.Event] = {}
    self._available: dict[str, asyncio.Event] = {}
    self._tasks: set[asyncio.Task] = set()
    self._manual_tasks: set[asyncio.Task] = set()
    self._watchers: set[asyncio.Task] = set()
    self._inflight: set[asyncio.Task] = set()
    self._active: set[str] = set()
    self._channels = contextlib.AsyncExitStack()
    self._manual: dict[Peer, str] = {}
    self._manual_states: dict[str, ManualApplier] = {}
    self._staging: Path | None = None
    self._closing = False

  @contextlib.asynccontextmanager
  async def lifetime(self) -> AsyncGenerator[None]:
    async with self._channels:
      with tempfile.TemporaryDirectory(prefix='ride-credential-operations-') as temporary:
        self._staging = Path(temporary)
        try:
          yield
        finally:
          self._closing = True
          tasks = list(self._tasks)
          inflight = list(self._inflight)
          manual = list(self._manual_tasks)
          for task in tasks:
            if task not in self._manual_tasks:
              task.cancel()
          await asyncio.gather(
            *(task for task in tasks if task not in manual), return_exceptions=True
          )
          await asyncio.gather(*inflight, return_exceptions=True)
          await asyncio.gather(*manual, return_exceptions=True)
          for mission_id in list(self._pending):
            self.settle(mission_id, ended=True)
          watchers = list(self._watchers)
          for watcher in watchers:
            watcher.cancel()
          await asyncio.gather(*watchers, return_exceptions=True)

  def _record(self, facts: WorkerFacts, operation: dict[str, Any], *, commit: bool) -> None:
    directory = records_directory(facts)
    resume = directory / 'resume.json'
    path = resume if facts.member is None and resume.exists() else directory / 'override.json'
    if path.exists() and facts.member is None:
      data = json.loads(path.read_text())
      if commit:
        data.update(operation['values'])
      data['override_operation'] = operation
    else:
      data = {'override_operation': operation}
    atomic_record(path, data)

  def handle(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    try:
      if self._closing:
        raise ValueError('override check: the root is ending')
      requested = changes(message.args.get('changes'))
      if all(name.startswith('@') for _, name in requested):
        value = apply_authority(self.peers, self.journal, self.reading, message.args)
        context.reply(peer, {'outcome': 'ok', 'value': value})
        return
      if set(message.args) != {'id', 'changes'} or not isinstance(message.args['id'], str):
        raise ValueError('override check: id and changes required')
      mission_id = message.args['id']
      if mission_id not in {row['id'] for row in self.peers.missions()}:
        raise ValueError('override check: id does not identify a mission of this root')
      facts = self.peers.for_mission(mission_id)
      record = self.journal.records[mission_id]
      if record.state != 'started' or facts.type != 'bro' or not isinstance(facts.record, Mission):
        raise ValueError('override check: target is not a running bro session')
      if facts.pending_override is not None:
        raise ValueError(f'override check: operation {facts.pending_override["id"]} is pending')
      if facts.expected:
        state = self._manual_states.get(mission_id)
        if state is None or not state.alive:
          raise ValueError('override check: the manual credential applier is unavailable')
      proposed = candidate(facts.record, requested, self.reading)
      changed = {name.split('+', 1)[0] for _, name in requested if not name.startswith('@')}
      selection = {
        name.removeprefix('cred.'): cast(str, value)
        for name, value in proposed.descriptor.values.items()
        if name.startswith('cred.')
      }
      selected = ScopedSecrets(set(selection) & changed, set(), selection)
      files, _ = hydrate(selected)
      operation = {
        'id': lulid(),
        'kind': 'credentials',
        'state': 'pending',
        'changes': [[verb, name] for verb, name in requested],
        'values': {
          'authority_request': dump_request(proposed.request, proposed.descriptor),
          'operator_values': sorted(proposed.operator_values),
          'operator_gift': dump_authority(proposed.operator_gift, proposed.descriptor),
          'operator_cap': dump_authority(proposed.operator_cap, proposed.descriptor),
        },
      }
      request = {
        'operation': operation['id'],
        'changed': sorted(changed),
        'files': {name: base64.b64encode(value).decode() for name, value in files.items()},
      }
      facts.pending_override = operation
      try:
        self._record(facts, operation, commit=False)
      except BaseException:
        facts.pending_override = None
        raise
      self._pending[mission_id] = proposed, operation, request
      self._available.setdefault(mission_id, asyncio.Event()).set()
      signal = self._signals.setdefault(mission_id, asyncio.Event())
      signal.clear()
      task = asyncio.create_task(self._apply(mission_id))
      self._tasks.add(task)
      task.add_done_callback(self._tasks.discard)
      if facts.expected:
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
      context.defer(peer, self._answer(mission_id, proposed, operation))
    except (ValueError, OSError, UnattributablePeer, credentials.SecretNotFound) as error:
      context.reply(peer, {'outcome': 'denied', 'error': str(error)})

  async def _answer(self, mission_id: str, proposed: Mission, operation: dict) -> dict[str, Any]:
    try:
      async with asyncio.timeout(ACK_TIMEOUT):
        await self._signals[mission_id].wait()
    except TimeoutError:
      status = 'uncertain'
    else:
      status = 'applied' if operation['state'] == 'applied' else 'dropped'
    kinds = sorted(
      {name.split('+', 1)[0] for _, name in operation['changes'] if not name.startswith('@')}
    )
    next_start = {}
    harness = cast(str, proposed.descriptor.values['harness'])
    driver = self.reading.installation.harnesses[harness]
    provider = cast(str, proposed.descriptor.values['provider'])
    for kind in kinds:
      effects = ['tool and feature selection']
      if kind == driver.auth_kind:
        effects.append(f'{harness} authentication')
      if driver.llm_key and kind in self.reading.installation.provider_needs[provider]:
        effects.append('the bro harness LLM client')
      if kind == 'trails':
        effects.append('the recorder trails client')
      next_start[kind] = effects
    return {
      'outcome': 'ok',
      'value': {
        'operation': operation['id'],
        'status': status,
        'changes': operation['changes'],
        'next_start': next_start,
        'may_summon': mission_view(proposed)['may_summon'],
      },
    }

  async def _apply(self, mission_id: str) -> None:
    facts = self.peers.for_mission(mission_id)
    while mission_id in self._pending:
      _, operation, request = self._pending[mission_id]
      if facts.credential_applier is not None:
        try:
          self._active.add(mission_id)
          work = asyncio.create_task(
            asyncio.to_thread(
              facts.credential_applier.apply,
              request,
              receipt_path(records_directory(facts), operation['id']),
            )
          )
          self._inflight.add(work)
          work.add_done_callback(self._inflight.discard)
          await asyncio.shield(work)
          current = self.journal.records.get(mission_id)
          self.settle(mission_id, ended=current is None or current.state != 'started')
        except (
          OSError,
          RuntimeError,
          TimeoutError,
          ValueError,
          subprocess.SubprocessError,
        ) as error:
          log.warning('credential operation %s awaiting applier: %s', operation['id'], error)
        self._active.discard(mission_id)
      elif receipt(records_directory(facts), operation['id']) is not None:
        self.settle(mission_id)
      record = self.journal.records.get(mission_id)
      if (
        (self._closing or record is None or record.state != 'started')
        and mission_id not in self._active
        and not self._manual_running(mission_id)
      ):
        self.settle(mission_id, ended=True)
        return
      if mission_id in self._pending:
        await asyncio.sleep(1)

  def settle(self, mission_id: str, *, ended: bool = False) -> None:
    if mission_id not in self._pending:
      return
    proposed, operation, _ = self._pending[mission_id]
    facts = self.peers.for_mission(mission_id)
    if ended and facts.member is not None:
      (records_directory(facts) / 'override.json').unlink(missing_ok=True)
      operation['state'] = 'dropped'
      facts.pending_override = None
      self._pending.pop(mission_id)
      self._signals[mission_id].set()
      return
    acknowledged = receipt(records_directory(facts), operation['id'])
    if acknowledged is None and not ended:
      return
    state = 'applied' if acknowledged else 'dropped'
    settled = {**operation, 'state': state}
    self._record(facts, settled, commit=acknowledged is not None)
    operation['state'] = state
    if acknowledged is not None:
      facts.record = proposed
      self.journal.override(
        self.journal.records[mission_id],
        {
          'operation': operation['id'],
          'status': state,
          'changes': operation['changes'],
          'generation': acknowledged['generation'],
          'may_summon': mission_view(proposed)['may_summon'],
        },
      )
    facts.pending_override = None
    self._pending.pop(mission_id)
    state = self._manual_states.get(mission_id)
    if state is not None:
      state.claimed = False
    self._signals[mission_id].set()
    if self._staging is not None:
      (self._staging / (operation['id'] + '.json')).unlink(missing_ok=True)

  def observe(self, event: Event, record: Record) -> None:
    if (
      event.transition == 'ended'
      and event.mission in self._pending
      and event.mission not in self._active
      and not self._manual_running(event.mission)
    ):
      self.settle(event.mission, ended=True)

  def register(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    token = message.args.get('token')
    try:
      if self._closing:
        raise ValueError('applier check: the root is ending')
      if not isinstance(token, str):
        raise ValueError('applier check: string token required')
      facts = self.peers.for_mission(token)
      previous = self._manual_states.get(token)
      if previous is not None and previous.alive:
        raise ValueError('applier check: mission already has a manual applier')
      if set(message.args) != {'token'} or not facts.expected or facts.type != 'bro':
        raise ValueError('applier check: expected bro token required')
    except (ValueError, UnattributablePeer) as error:
      context.reply(peer, {'outcome': 'denied', 'error': str(error)})
      return

    async def provision() -> dict[str, Any]:
      provisioned = await self._channels.enter_async_context(context.request_channel())
      self._manual[provisioned.channel] = token
      if self._staging is None:
        raise RuntimeError('manual applier has no root lifetime')
      path = self._staging / (lulid() + '.lifetime')
      os.mkfifo(path, 0o600)
      descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
      stream = self._channels.enter_context(os.fdopen(descriptor, 'rb', buffering=0))
      self._manual_states[token] = ManualApplier(path, stream)
      return {
        'outcome': 'ok',
        'value': {'address': provisioned.host_endpoint.address(LOCAL_HOST), 'lifeline': str(path)},
      }

    context.defer(peer, provision())

  def work(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    mission_id = self._manual.get(peer)
    if mission_id is None or message.args:
      context.reply(peer, {'outcome': 'denied', 'error': 'applier channel required'})
      return

    state = self._manual_states[mission_id]
    if not state.alive:
      context.reply(peer, {'outcome': 'denied', 'error': 'manual applier is not attached'})
      return

    async def next_request() -> dict[str, Any]:
      if mission_id not in self._pending:
        available = self._available.setdefault(mission_id, asyncio.Event())
        available.clear()
        try:
          async with asyncio.timeout(10):
            await available.wait()
        except TimeoutError:
          return {'outcome': 'ok', 'value': None}
      pending = self._pending.get(mission_id)
      if pending is None:
        return {'outcome': 'ok', 'value': None}
      state.claimed = True
      if self._staging is None:
        raise RuntimeError('credential override control has no staging lifetime')
      path = self._staging / (pending[1]['id'] + '.json')
      if not path.exists():
        atomic_file(path, json.dumps(pending[2]).encode())
      return {'outcome': 'ok', 'value': {'operation': pending[1]['id'], 'request_file': str(path)}}

    context.defer(peer, next_request())

  def acknowledge(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    mission_id = self._manual.get(peer)
    if mission_id is None:
      context.reply(peer, {'outcome': 'denied', 'error': 'applier channel required'})
      return
    pending = self._pending.get(mission_id)
    if pending is not None and message.args == {'operation': pending[1]['id']}:
      self._active.discard(mission_id)
      self.settle(mission_id)
    context.reply(peer, {'outcome': 'ok'})

  def close(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    mission_id = self._manual.get(peer)
    if mission_id is None or message.args:
      context.reply(peer, {'outcome': 'denied', 'error': 'applier channel required'})
      return
    self._active.discard(mission_id)
    self._manual_states[mission_id].claimed = False
    record = self.journal.records.get(mission_id)
    self.settle(mission_id, ended=record is None or record.state != 'started')
    context.reply(peer, {'outcome': 'ok'})

  def _manual_running(self, mission_id: str) -> bool:
    state = self._manual_states.get(mission_id)
    return state is not None and state.alive and state.claimed

  def attach(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    mission_id = self._manual.get(peer)
    if mission_id is None or message.args:
      context.reply(peer, {'outcome': 'denied', 'error': 'applier channel required'})
      return
    state = self._manual_states[mission_id]
    if state.alive:
      context.reply(peer, {'outcome': 'denied', 'error': 'manual applier is already attached'})
      return

    async def attached() -> dict[str, Any]:
      reader = asyncio.StreamReader()
      transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        state.stream,
      )
      self._channels.callback(transport.close)
      if await reader.readexactly(len(LIFETIME_READY)) != LIFETIME_READY:
        raise ValueError('manual applier lifetime handshake failed')
      state.alive = True
      task = asyncio.create_task(self._watch_manual(mission_id, state, reader))
      self._watchers.add(task)
      task.add_done_callback(self._watchers.discard)
      return {'outcome': 'ok'}

    context.defer(peer, attached())

  async def _watch_manual(
    self, mission_id: str, state: ManualApplier, reader: asyncio.StreamReader
  ) -> None:
    if await reader.read(1) != b'':
      raise ValueError('unexpected data on the manual applier lifetime pipe')
    state.alive = False
    if self._manual_states.get(mission_id) is not state:
      return
    record = self.journal.records.get(mission_id)
    self.settle(mission_id, ended=self._closing or record is None or record.state != 'started')
