"""peer-side handle over one channel back to the host broker.

Synchronous throughout — a peer is its own process with no event loop; only the
host-side broker is async. `from_env()` resolves the channel from `BROKER_CHANNEL`
and returns `None` when it is unset, so consumers (the `broker` CLI, the bro hook)
are inert where there is no channel.

`request` and `call` are correlate-on-receive: they send the request, then read
inbound messages until one carries `in_reply_to == request.id`. `request` returns
the first correlated message; `call` rides through interim `started` messages
(surfaced to a callback) and returns the first correlated terminal. Uncorrelated
arrivals are set aside and handed out by later `receive` calls rather than dropped.
No reader thread — concurrent in-flight requests are a consumer need that has not
arisen.

`send` returns the sent message (ids are minted client-side) and `await_reply` is
`call`'s wait detached from its send, so a consumer can expose the request id the
moment it is on the wire and block — or reattach — separately.
"""

import os
import time
from collections import deque
from collections.abc import Callable
from types import TracebackType
from typing import Any, Optional

from broker.brotocol import Message, Tag
from broker.transport import ClientTransport, connect

CHANNEL_ENV = 'BROKER_CHANNEL'


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

  def send(self, type: str, payload: dict[str, Any]) -> Message:
    """send a fresh typed message and return it — the id is minted client-side, so
    the caller can print or persist it before (or instead of) awaiting the reply."""
    message = Message(type=type, payload=payload)
    self._transport.send(message)
    return message

  def request(self, type: str, payload: dict[str, Any], timeout: Optional[float]) -> Message:
    """send a typed request and block for the first message correlated to it.

    Raises TimeoutError when `timeout` seconds pass without a correlated message,
    ConnectionError when the channel reaches EOF first.
    """
    request = self.send(type, payload)
    deadline = time.monotonic() + timeout if timeout is not None else None
    return self._receive_correlated(request, deadline, timeout)

  def call(
    self,
    type: str,
    payload: dict[str, Any],
    timeout: Optional[float],
    *,
    on_started: Optional[Callable[[Message], None]] = None,
  ) -> Message:
    """send a typed request and block for the first correlated terminal message.

    Interim correlated `started` messages are surfaced to `on_started` and the wait
    continues; any other correlated message (`completed` / `failed` / `reply`) is the
    terminal and is returned. `timeout` bounds the whole call, interims included.
    Raises as `request` does.
    """
    return self.await_reply(self.send(type, payload), timeout, on_started=on_started)

  def await_reply(
    self,
    request: Message,
    timeout: Optional[float],
    *,
    on_started: Optional[Callable[[Message], None]] = None,
    timeout_after_started: Optional[float] = None,
  ) -> Message:
    """block for the first terminal correlated to an already-sent `request` — the
    detached tail of `call`, for a caller that sent first (to expose the request id)
    and awaits separately. Semantics and errors are exactly `call`'s wait, except
    when `timeout_after_started` is set: each correlated `started` then re-arms the
    deadline to that many seconds from its arrival, so the bound covers the silence
    since the last message rather than the whole wait."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
      message = self._receive_correlated(request, deadline, timeout)
      if message.type != Tag.STARTED:
        return message
      if timeout_after_started is not None:
        deadline = time.monotonic() + timeout_after_started
        timeout = timeout_after_started
      if on_started is not None:
        on_started(message)

  def _receive_correlated(
    self, request: Message, deadline: Optional[float], timeout: Optional[float]
  ) -> Message:
    """read until a message correlates to `request`, setting uncorrelated arrivals aside."""
    while True:
      remaining = None
      if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise TimeoutError(f'no reply to {request.type!r} request {request.id} within {timeout}s')
      message = self._transport.receive(remaining)
      if message is None:
        # the transport returns None for both timeout and EOF; the deadline says which
        if deadline is not None and time.monotonic() >= deadline:
          raise TimeoutError(f'no reply to {request.type!r} request {request.id} within {timeout}s')
        raise ConnectionError(f'broker channel closed awaiting reply to {request.type!r} request')
      if message.in_reply_to == request.id:
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
