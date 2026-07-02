"""peer-side handle over one channel back to the host broker.

Synchronous throughout — a peer is its own process with no event loop; only the
host-side broker is async. `from_env()` resolves the channel from `BROKER_CHANNEL`
and returns `None` when it is unset, so consumers (the `broker` CLI, the bro hook)
are inert where there is no channel.

`request` is correlate-on-receive: it sends the request, then reads inbound
messages until one carries `in_reply_to == request.id`. Uncorrelated arrivals are
set aside and handed out by later `receive` calls rather than dropped. No reader
thread — concurrent in-flight requests are a consumer need that has not arisen.
"""

import os
import time
from collections import deque
from typing import Any, Optional

from broker.brotocol import Message
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

  def send(self, type: str, payload: dict[str, Any]) -> None:
    self._transport.send(Message(type=type, payload=payload))

  def request(self, type: str, payload: dict[str, Any], timeout: Optional[float]) -> Message:
    """send a typed request and block for the first message correlated to it.

    Raises TimeoutError when `timeout` seconds pass without a correlated message,
    ConnectionError when the channel reaches EOF first.
    """
    request = Message(type=type, payload=payload)
    self._transport.send(request)
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
      remaining = None
      if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise TimeoutError(f'no reply to {type!r} request {request.id} within {timeout}s')
      message = self._transport.receive(remaining)
      if message is None:
        # the transport returns None for both timeout and EOF; the deadline says which
        if deadline is not None and time.monotonic() >= deadline:
          raise TimeoutError(f'no reply to {type!r} request {request.id} within {timeout}s')
        raise ConnectionError(f'broker channel closed awaiting reply to {type!r} request')
      if message.in_reply_to == request.id:
        return message
      # a message set aside here can never correlate to a future request (its id
      # does not exist yet), so request() never has to scan the set-aside queue
      self._set_aside.append(message)

  def receive(self, timeout: Optional[float]) -> Optional[Message]:
    if len(self._set_aside) > 0:
      return self._set_aside.popleft()
    return self._transport.receive(timeout)

  def close(self) -> None:
    self._transport.close()
