"""peer-side handle over one channel back to the host broker.

Synchronous throughout — a peer is its own process with no event loop; only the
host-side broker is async. `from_env()` resolves the channel from `BROKER_CHANNEL`
and returns `None` when it is unset, so consumers (the `broker` CLI, the bro hook)
are inert where there is no channel.

`request` and `call` are correlate-on-receive:
they send a request, then read inbound messages until one names the quest opened by the request.
`request` returns the first correlated message;
`call` rides through marks and progress (surfaced to a callback) and returns the correlated result. Uncorrelated
arrivals are set aside and handed out by later `receive` calls rather than
dropped. No reader thread — concurrent in-flight requests are a consumer need
that has not arisen.

`send` returns the sent request (ids are minted client-side); `await_reply` is
`call`'s wait detached from its send and `await_any` is `request`'s, so a
consumer can expose the request id the moment it is on the wire and block — or
reattach — separately. `progress` and `result` are the answering side: a worker
peer emits them against the quest id its launch carried (`QUEST_ENV`).
"""

import os
import time
from collections import deque
from collections.abc import Callable
from types import TracebackType
from typing import Any, Optional

from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag
from bro.broker.transport import ClientTransport, connect

CHANNEL_ENV = 'BROKER_CHANNEL'
# the quest a launched peer answers, set beside CHANNEL_ENV by whatever
# launches it (the host's spawner adapters, a manual summon's launch surface)
QUEST_ENV = 'BROKER_QUEST'


class Client:
  def __init__(self, transport: ClientTransport):
    self._transport = transport
    self._set_aside: deque[Message] = deque()  # uncorrelated arrivals read during request()

  @classmethod
  def from_env(cls) -> Optional['Client']:
    address = os.environ.get(CHANNEL_ENV)
    if address is None:
      return None
    return cls(connect(address))

  def send(self, kind: str, args: dict[str, Any]) -> Message:
    """send a fresh request and return it — the id is minted client-side, so the
    caller can print or persist it before (or instead of) awaiting the reply."""
    message = brotocol.request(kind, args)
    self._transport.send(message)
    return message

  def progress(self, quest_id: str, payload: dict[str, Any]) -> None:
    """emit progress on `quest_id` from its worker peer."""
    self._transport.send(brotocol.progress(quest_id, payload))

  def result(self, quest_id: str, payload: dict[str, Any]) -> None:
    """emit the result closing `quest_id` from its worker peer."""
    self._transport.send(Message(type=Tag.RESULT, payload=payload, quest=quest_id))

  def request(self, kind: str, args: dict[str, Any], timeout: Optional[float]) -> Message:
    """send a request and block for the first message correlated to it.

    Raises TimeoutError when `timeout` seconds pass without a correlated message,
    ConnectionError when the channel reaches EOF first.
    """
    request = self.send(kind, args)
    deadline = time.monotonic() + timeout if timeout is not None else None
    return self._receive_correlated(request, deadline, timeout)

  def call(
    self,
    kind: str,
    args: dict[str, Any],
    timeout: Optional[float],
    *,
    on_interim: Optional[Callable[[Message], None]] = None,
  ) -> Message:
    """send a request and block for the result correlated to it.

    Correlated marks and progress are surfaced to `on_interim` and the wait continues.
    The correlated result is returned.
    `timeout` bounds the whole call, interim messages included.
    Raises as `request` does.
    """
    return self.await_reply(self.send(kind, args), timeout, on_interim=on_interim)

  def await_reply(
    self,
    request: Message,
    timeout: Optional[float],
    *,
    on_interim: Optional[Callable[[Message], None]] = None,
    timeout_after_interim: Optional[float] = None,
  ) -> Message:
    """block for the result correlated to an already-sent `request` — the detached
    tail of `call`, for a caller that sent first (to expose the request id) and
    awaits separately. Semantics and errors are exactly `call`'s wait, except when
    `timeout_after_interim` is set:
    each correlated mark or progress message then re-arms the deadline to that many seconds from its arrival, so the bound covers the silence since the last message rather than the whole wait."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
      message = self._receive_correlated(request, deadline, timeout)
      if message.type == Tag.RESULT:
        return message
      if timeout_after_interim is not None:
        deadline = time.monotonic() + timeout_after_interim
        timeout = timeout_after_interim
      if on_interim is not None:
        on_interim(message)

  def await_any(self, request: Message, timeout: Optional[float]) -> Message:
    """block for the first mark, progress, or result correlated to `request`."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    return self._receive_correlated(request, deadline, timeout)

  def _receive_correlated(
    self, request: Message, deadline: Optional[float], timeout: Optional[float]
  ) -> Message:
    """read until a message correlates to `request`, setting uncorrelated arrivals aside."""
    while True:
      remaining = None
      if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise TimeoutError(f'no reply to {request.kind!r} request {request.id} within {timeout}s')
      message = self._transport.receive(remaining)
      if message is None:
        # the transport returns None for both timeout and EOF; the deadline says which
        if deadline is not None and time.monotonic() >= deadline:
          raise TimeoutError(f'no reply to {request.kind!r} request {request.id} within {timeout}s')
        raise ConnectionError(f'broker channel closed awaiting reply to {request.kind!r} request')
      if message.quest_id == request.id:
        return message
      # a message set aside here can never correlate to a future request (its id
      # does not exist yet), so this loop never has to scan the set-aside queue
      self._set_aside.append(message)

  def receive(self, timeout: Optional[float]) -> Optional[Message]:
    if len(self._set_aside) > 0:
      return self._set_aside.popleft()
    return self._transport.receive(timeout)

  def close(self, confirm: bool = False) -> None:
    """close the channel; `confirm` semantics per `ClientTransport.close`."""
    self._transport.close(confirm)

  def __enter__(self) -> 'Client':
    return self

  def __exit__(
    self,
    exception_type: Optional[type[BaseException]],
    exception: Optional[BaseException],
    traceback: Optional[TracebackType],
  ) -> None:
    self.close()
