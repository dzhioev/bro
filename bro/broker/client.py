"""peer-side handle over one channel back to the host broker.

Synchronous throughout — a peer is its own process with no event loop; only the
host-side broker is async. `from_env()` resolves the client address from
`BROKER_CHANNEL`, returns `None` when neither broker variable is set, and reports
a failed session proxy when only `BROKER_UPSTREAM` remains.

`request` and `call` are correlate-on-receive:
they send a request, then read inbound messages until one names the quest opened by the request.
`request` returns the first correlated message;
`call` rides through marks and messages (surfaced to a callback) and returns the correlated result.
Uncorrelated arrivals are set aside and handed out by later `receive` calls rather than dropped.
No reader thread — concurrent in-flight requests are a consumer need that has not arisen.

`send` returns the sent request (ids are minted client-side); `await_reply` is `call`'s wait detached from its send and `await_any` is `request`'s, so a consumer can expose the request id the moment it is on the wire and block — or reattach — separately.
`message` sends chat traffic, `await_reply_to` correlates a reply by message id, and `listen` registers the connection for unsolicited quest messages.
`mark` and `result` are the answering-side lifecycle calls;
a worker peer emits them against the quest id its launch carried (`QUEST_ENV`), with its fixed chat rights in `BROKER_TALK`.
"""

import os
import time
from collections import deque
from collections.abc import Callable
from types import TracebackType
from typing import Any, Optional

from bro.base.lulid import lulid
from bro.broker import brotocol
from bro.broker.brotocol import Message, Tag, Talk
from bro.broker.transport import ClientTransport, connect
from bro.launch.broker_environment import CHANNEL_ENV, UPSTREAM_ENV, broxy_log_path

# the quest a launched peer answers, set beside CHANNEL_ENV by whatever
# launches it (the host's spawner adapters, a manual summon's launch surface)
QUEST_ENV = 'BROKER_QUEST'


def talk_from_env() -> Optional[Talk]:
  """Read this peer's quest talk, or None when its launcher published no talk."""
  value = os.environ.get(brotocol.TALK_ENV)
  return None if value is None else brotocol.decode_talk(value)


def _missing_summoned_right(talk: Talk, message: Message) -> str:
  if message.is_say:
    return 'summoned.say'
  if message.is_reply and 'summoner.question' not in talk:
    return 'summoner.question'
  if message.is_question and 'summoned.question' not in talk:
    return 'summoned.question'
  raise RuntimeError('allowed message has no missing talk right')


class ReplyDeadline:
  """when a reply wait expires: `timeout` seconds from `now`, re-armed to `after_interim`
  seconds from each interim it observes — every one, or only those `rearms_on` admits.
  `timeout` is the bound in force, what an expiry reports."""

  def __init__(
    self,
    timeout: Optional[float],
    now: float,
    *,
    after_interim: Optional[float] = None,
    rearms_on: Optional[Callable[[Message], bool]] = None,
  ):
    self.timeout = timeout
    self._expiry = now + timeout if timeout is not None else None
    self._after_interim = after_interim
    self._rearms_on = rearms_on

  def observe(self, interim: Message, now: float) -> None:
    if self._after_interim is None:
      return
    if self._rearms_on is not None and not self._rearms_on(interim):
      return
    self.timeout = self._after_interim
    self._expiry = now + self._after_interim

  def remaining(self, now: float) -> Optional[float]:
    """seconds until the expiry, None while unbounded."""
    return None if self._expiry is None else self._expiry - now

  def expired(self, now: float) -> bool:
    return self._expiry is not None and now >= self._expiry


class Client:
  def __init__(self, transport: ClientTransport):
    self._transport = transport
    self._set_aside: deque[Message] = deque()  # uncorrelated arrivals read during request()

  @classmethod
  def from_env(cls) -> Optional['Client']:
    address = os.environ.get(CHANNEL_ENV)
    if address is not None:
      return cls(connect(address))
    if os.environ.get(UPSTREAM_ENV) is None:
      return None
    raise RuntimeError(
      f'session proxy failed at launch; see the broxy log at {broxy_log_path(os.environ)}'
    )

  def send(self, kind: str, args: dict[str, Any]) -> Message:
    """send a fresh request and return it — the id is minted client-side, so the
    caller can print or persist it before (or instead of) awaiting the reply."""
    message = brotocol.request(kind, args)
    self._transport.send(message)
    return message

  def mark(self, quest_id: str, transition: str, **payload: Any) -> None:
    """emit a lifecycle mark on ``quest_id`` from its worker peer."""
    self._transport.send(brotocol.mark(quest_id, transition, **payload))

  def message(
    self,
    quest_id: str,
    payload: dict[str, Any],
    *,
    reply_to: Optional[str] = None,
    question: bool = False,
  ) -> Message:
    """Send chat traffic on a quest and return the sent envelope."""
    message = brotocol.message(
      quest_id,
      payload,
      id=lulid() if question else None,
      reply_to=reply_to,
    )
    self._require_own_quest_talk(message)
    self._transport.send(message)
    return message

  def listen(self, quest_id: str) -> None:
    """Register this connection for unsolicited messages on `quest_id`."""
    self.mark(quest_id, 'listening')

  def result(self, quest_id: str, payload: dict[str, Any]) -> None:
    """emit the result closing `quest_id` from its worker peer."""
    self._transport.send(Message(type=Tag.RESULT, payload=payload, quest=quest_id))

  def request(self, kind: str, args: dict[str, Any], timeout: Optional[float]) -> Message:
    """send a request and block for the first message correlated to it.

    Raises TimeoutError when `timeout` seconds pass without a correlated message,
    ConnectionError when the channel reaches EOF first.
    """
    request = self.send(kind, args)
    return self._receive_correlated(request, ReplyDeadline(timeout, time.monotonic()))

  def call(
    self,
    kind: str,
    args: dict[str, Any],
    timeout: Optional[float],
    *,
    on_interim: Optional[Callable[[Message], None]] = None,
  ) -> Message:
    """send a request and block for the result correlated to it.

    Correlated marks and messages are surfaced to `on_interim` and the wait continues.
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
    rearm_on_interim: Optional[Callable[[Message], bool]] = None,
    until: Optional[Callable[[Message], bool]] = None,
  ) -> Message:
    """block for the result correlated to an already-sent `request` — the detached
    tail of `call`, for a caller that sent first (to expose the request id) and
    awaits separately. Semantics and errors are exactly `call`'s wait, except when
    `timeout_after_interim` is set:
    correlated interim messages re-arm the deadline to that many seconds from arrival.
    `rearm_on_interim` narrows which interim messages trigger that re-arm.
    `until` returns a matching interim message instead of waiting for the result."""
    deadline = ReplyDeadline(
      timeout, time.monotonic(), after_interim=timeout_after_interim, rearms_on=rearm_on_interim
    )
    while True:
      message = self._receive_correlated(request, deadline)
      if message.type == Tag.RESULT or (until is not None and until(message)):
        return message
      deadline.observe(message, time.monotonic())
      if on_interim is not None:
        on_interim(message)

  def await_reply_to(self, question: Message, timeout: Optional[float]) -> Message:
    """Block for the chat message whose `reply_to` names `question`."""
    if question.type != Tag.MESSAGE or question.id is None:
      raise ValueError('await_reply_to needs a question message with an id')
    return self._receive_matching(
      lambda message: message.reply_to == question.id,
      ReplyDeadline(timeout, time.monotonic()),
      f'message {question.id}',
    )

  def await_any(self, request: Message, timeout: Optional[float]) -> Message:
    """block for the first correlated envelope correlated to `request`."""
    return self._receive_correlated(request, ReplyDeadline(timeout, time.monotonic()))

  def _receive_correlated(self, request: Message, deadline: ReplyDeadline) -> Message:
    return self._receive_matching(
      lambda message: message.quest_id == request.id,
      deadline,
      f'{request.kind!r} request {request.id}',
    )

  def _receive_matching(
    self, matches: Callable[[Message], bool], deadline: ReplyDeadline, awaited: str
  ) -> Message:
    """read until a message `matches`, setting the others aside; `awaited` names the reply in errors."""
    buffered = next((message for message in self._set_aside if matches(message)), None)
    if buffered is not None:
      self._set_aside.remove(buffered)
      return buffered
    while True:
      now = time.monotonic()
      if deadline.expired(now):
        raise TimeoutError(f'no reply to {awaited} within {deadline.timeout}s')
      message = self._transport.receive(deadline.remaining(now))
      if message is None:
        # the transport returns None for both timeout and EOF; the deadline says which
        if deadline.expired(time.monotonic()):
          raise TimeoutError(f'no reply to {awaited} within {deadline.timeout}s')
        raise ConnectionError(f'broker channel closed awaiting reply to {awaited}')
      if matches(message):
        return message
      self._set_aside.append(message)

  @staticmethod
  def _require_own_quest_talk(message: Message) -> None:
    if message.quest_id != os.environ.get(QUEST_ENV):
      return
    talk = talk_from_env()
    if talk is None or brotocol.message_allowed(talk, 'summoned', message):
      return
    missing_right = _missing_summoned_right(talk, message)
    raise PermissionError(
      f'{brotocol.TALK_ENV} lacks {missing_right} for a message on the session quest'
    )

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
