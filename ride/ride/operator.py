"""Operator access to a running root through its private control record."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import AsyncGenerator, Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bro.workspace.paths import ensure_runtime_root, is_workspace_name, runtime_base

if TYPE_CHECKING:
  from bro.broker.brotocol import Message
  from bro.broker.dispatcher import Broker, Dispatcher, RequestHandler
  from bro.broker.runtime import Peer
  from ride.peer_facts import PeerFacts

MISSIONS = 'operator.missions'


def _directory() -> Path:
  return runtime_base() / 'control'


def _path(root: str) -> Path:
  if not is_workspace_name(root):
    raise ValueError(f'not a usable root name: {root!r}')
  return _directory() / f'{root}.json'


@dataclass(frozen=True)
class ControlRecord:
  address: str
  runtime: str


def read_control(root: str) -> ControlRecord:
  try:
    data = json.loads(_path(root).read_text())
  except FileNotFoundError:
    raise ValueError(f'root {root!r} has no operator control file') from None
  if not isinstance(data, dict) or set(data) != {'address', 'runtime'}:
    raise ValueError(f'root {root!r} has an invalid operator control file')
  if not all(isinstance(value, str) and value for value in data.values()):
    raise ValueError(f'root {root!r} has an invalid operator control file')
  from bro.broker.transports.tcp import parse_address

  parse_address(data['address'])
  return ControlRecord(**data)


@contextlib.contextmanager
def _directory_lock() -> Generator[None]:
  ensure_runtime_root()
  directory = _directory()
  directory.mkdir(mode=0o700, exist_ok=True)
  with (directory / '.lock').open('a') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    yield


@contextlib.contextmanager
def _try_record_lock(path: Path) -> Generator[bool]:
  with path.open() as stream:
    try:
      fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      yield False
    else:
      yield True


@contextlib.contextmanager
def publish_control(root: str, record: ControlRecord) -> Generator[None]:
  path = _path(root)
  with contextlib.ExitStack() as stack:
    with _directory_lock():
      if path.exists():
        with _try_record_lock(path) as unlocked:
          if not unlocked:
            raise RuntimeError(f'root {root!r} already has an active operator channel')
      descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix='.control-')
      temporary = Path(temporary_name)
      stack.callback(temporary.unlink, missing_ok=True)
      stream = stack.enter_context(os.fdopen(descriptor, 'w'))
      fcntl.flock(stream, fcntl.LOCK_EX)
      json.dump(asdict(record), stream)
      stream.flush()
      os.replace(temporary, path)
    try:
      yield
    finally:
      with _directory_lock():
        path.unlink()


def clean_controls(*, dry_run: bool = False) -> tuple[int, int]:
  removed = active = 0
  with _directory_lock():
    paths = [*_directory().glob('*.json'), *_directory().glob('.control-*')]
    for path in sorted(paths):
      with _try_record_lock(path) as unlocked:
        if not unlocked:
          active += 1
        else:
          if not dry_run:
            path.unlink()
          removed += 1
  return removed, active


class OperatorControl:
  def __init__(self, broker: Broker, *, root: str, runtime: str, peers: PeerFacts):
    self._broker = broker
    self._root = root
    self._runtime = runtime
    self._peers = peers
    self._peer: Peer | None = None
    self.on(MISSIONS, self._missions)

  def on(self, kind: str, handler: RequestHandler) -> None:
    def operator_only(context: Dispatcher, peer: Peer, message: Message) -> None:
      if self._peer is None or peer != self._peer:
        context.reply(peer, {'outcome': 'denied', 'error': 'operator channel required'})
        return
      handler(context, peer, message)

    self._broker.on(kind, operator_only)

  @contextlib.asynccontextmanager
  async def lifetime(self, context: Dispatcher) -> AsyncGenerator[None]:
    from bro.broker.transports.tcp import LOCAL_HOST, Endpoint

    async with context.request_channel() as provisioned:
      endpoint = provisioned.host_endpoint
      if not isinstance(endpoint, Endpoint):
        raise TypeError('operator channel needs a TCP endpoint')
      self._peer = provisioned.channel
      try:
        with publish_control(
          self._root, ControlRecord(endpoint.address(LOCAL_HOST), self._runtime)
        ):
          yield
      finally:
        self._peer = None

  def _missions(self, context: Dispatcher, peer: Peer, message: Message) -> None:
    from bro.broker import brotocol

    cursor = message.args.get('cursor', 0)
    if set(message.args) - {'cursor'} or type(cursor) is not int or cursor < 0:
      context.reply(peer, {'outcome': 'denied', 'error': 'invalid mission listing cursor'})
      return
    missions = self._peers.missions()
    if cursor > len(missions):
      context.reply(peer, {'outcome': 'denied', 'error': 'mission listing cursor out of range'})
      return
    page = []
    value: dict[str, Any] = {'missions': page}
    for position in range(cursor, len(missions)):
      page.append(missions[position])
      value['cursor'] = position + 1
      result = brotocol.result(message.request_id, 'ok', value=value)
      if len(result.to_bytes()) > brotocol.MAX_FRAME_BYTES:
        page.pop()
        value['cursor'] = position
        if not page:
          context.reply(peer, {'outcome': 'denied', 'error': 'mission listing row too large'})
          return
        break
    else:
      value.pop('cursor', None)
    context.reply(peer, {'outcome': 'ok', 'value': value})


def list_missions(root: str) -> int:
  from bro.broker.client import Client
  from bro.broker.transport import connect

  record = read_control(root)
  missions = []
  try:
    with Client(connect(record.address)) as client:
      args = {}
      while True:
        result = client.call(MISSIONS, args, timeout=30).payload
        if result.get('outcome') != 'ok':
          raise RuntimeError(f'root {root!r} refused mission listing: {result}')
        value = result['value']
        missions.extend(value['missions'])
        if 'cursor' not in value:
          break
        args = {'cursor': value['cursor']}
  except (ConnectionError, OSError, TimeoutError):
    raise RuntimeError(f'root {root!r} operator channel is unavailable') from None
  print(json.dumps(missions, indent=2))
  return 0
