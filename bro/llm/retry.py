"""Retrying an LLM call through a provider outage."""

from asyncio import sleep as asyncio_sleep
from collections.abc import Awaitable, Callable
from time import monotonic

from bro.base import log
from bro.base.retry import RetryPolicy, exponential_backoff, retry_async
from bro.llm.llm import LLMUnavailable

# long enough to outlast a brief outage between the host and the provider
RETRY_WINDOW_SECONDS = 180.0


async def retry_llm[T](
  request: Callable[[], Awaitable[T]],
  transient: Callable[[Exception], bool],
  *,
  capacity: Callable[[T], bool] | None = None,
  on_capacity: Callable[[T], Awaitable[None]] | None = None,
) -> T:
  """Retry transient failures and, when identified by `capacity`, refused replies.

  `on_capacity` receives each reply about to be retried, not the final reply.
  A repeat after a failure that reached the provider can create a second response,
  as the SDK's own retries can.
  """
  response = await _through_outage(request, transient)
  for attempt in range(6):
    if capacity is None or not capacity(response):
      break
    if on_capacity is not None:
      await on_capacity(response)
    delay = min(10 * 2**attempt, 300)
    log.warning('LLM provider at capacity; retrying in %d s (attempt %d)', delay, attempt + 2)
    await asyncio_sleep(delay)
    response = await _through_outage(request, transient)
  return response


async def _through_outage[T](
  request: Callable[[], Awaitable[T]], transient: Callable[[Exception], bool]
) -> T:
  policy = RetryPolicy(
    retryable=transient, delay=exponential_backoff(1.0, 15.0), window=RETRY_WINDOW_SECONDS
  )
  started = monotonic()
  try:
    return await retry_async(request, policy, label='LLM provider')
  except Exception as error:
    if not transient(error):
      raise
    elapsed = monotonic() - started
    raise LLMUnavailable(f'LLM provider unavailable for {elapsed:.0f} s: {error}') from error
