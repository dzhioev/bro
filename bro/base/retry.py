"""One retry loop, blocking and awaitable, over a caller's policy."""

from asyncio import sleep as asyncio_sleep
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Optional

from bro.base import log


@dataclass(frozen=True)
class RetryPolicy:
  """which failures to repeat a call after, how long to wait first, and when to stop.

  `delay(error, attempt)` is the wait after the 0-based `attempt` failed with `error`.
  The loop stops after `attempts` calls, and before any call that would start
  `window` seconds or more after the first.
  """

  retryable: Callable[[Exception], bool]
  delay: Callable[[Exception, int], float]
  attempts: Optional[int] = None
  window: Optional[float] = None

  def __post_init__(self) -> None:
    if self.attempts is None and self.window is None:
      raise ValueError('a retry policy needs an attempt count, a window, or both')
    if self.attempts is not None and self.attempts < 1:
      raise ValueError(f'a retry policy needs at least one attempt, got {self.attempts}')


def exponential_backoff(first: float, cap: float) -> Callable[[Exception, int], float]:
  """waits of `first` seconds, doubling each attempt, never above `cap`."""
  return lambda error, attempt: min(cap, first * 2**attempt)


def _wait(policy: RetryPolicy, error: Exception, attempt: int, started: float) -> Optional[float]:
  """the wait before the next attempt, or None when the policy gives up on `error`."""
  if not policy.retryable(error):
    return None
  if policy.attempts is not None and attempt + 1 >= policy.attempts:
    return None
  delay = policy.delay(error, attempt)
  if policy.window is not None and monotonic() - started + delay >= policy.window:
    return None
  return delay


def _announce(label: Optional[str], error: Exception, delay: float, attempt: int) -> None:
  if label is not None:
    log.warning('%s: %s; retrying in %.1f s (attempt %d)', label, error, delay, attempt + 2)


def retry[T](call: Callable[[], T], policy: RetryPolicy, *, label: Optional[str] = None) -> T:
  """`call()`, repeated per `policy`; the last failure propagates unchanged.
  With `label`, each repeat logs a warning naming it."""
  started = monotonic()
  attempt = 0
  while True:
    try:
      return call()
    except Exception as error:
      delay = _wait(policy, error, attempt, started)
      if delay is None:
        raise
      _announce(label, error, delay, attempt)
    sleep(delay)
    attempt += 1


async def retry_async[T](
  call: Callable[[], Awaitable[T]], policy: RetryPolicy, *, label: Optional[str] = None
) -> T:
  """`retry` for an awaitable call."""
  started = monotonic()
  attempt = 0
  while True:
    try:
      return await call()
    except Exception as error:
      delay = _wait(policy, error, attempt, started)
      if delay is None:
        raise
      _announce(label, error, delay, attempt)
    await asyncio_sleep(delay)
    attempt += 1
