"""Retrying an LLM call through a provider outage."""

from collections.abc import Awaitable, Callable
from time import monotonic

from bro.base.retry import RetryPolicy, exponential_backoff, retry_async
from bro.llm.llm import LLMUnavailable

# long enough to outlast a brief outage between the host and the provider
RETRY_WINDOW_SECONDS = 180.0


async def retry_llm[T](
  request: Callable[[], Awaitable[T]], transient: Callable[[Exception], bool]
) -> T:
  """await `request()`, repeated through transient failures until `RETRY_WINDOW_SECONDS`
  run out, then raise the provider-neutral `LLMUnavailable`.

  A repeat after a failure that reached the provider can create a second response,
  as the SDK's own retries can.
  """
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
