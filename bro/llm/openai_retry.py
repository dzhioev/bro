"""Retrying an OpenAI call through a provider outage."""

from collections.abc import Awaitable, Callable
from time import monotonic

from bro.base.retry import RetryPolicy, exponential_backoff, retry_async
from bro.llm.llm import LLMUnavailable

# long enough to outlast a brief outage between the host and the provider
RETRY_WINDOW_SECONDS = 180.0


def _transient(error: Exception) -> bool:
  """a connection failure or timeout, or a server error; a 429 stays out because
  OpenAI also answers it for an exhausted quota, which no wait clears."""
  from openai import APIConnectionError, APIStatusError

  if isinstance(error, APIConnectionError):
    return True
  return isinstance(error, APIStatusError) and error.status_code >= 500


_POLICY = RetryPolicy(
  retryable=_transient, delay=exponential_backoff(1.0, 15.0), window=RETRY_WINDOW_SECONDS
)


async def retry_openai[T](request: Callable[[], Awaitable[T]]) -> T:
  """await `request()`, repeated through transient failures until `RETRY_WINDOW_SECONDS`
  run out, then raise the provider-neutral `LLMUnavailable`.

  A repeat after a failure that reached the provider can create a second response,
  as the SDK's own retries can.
  """
  started = monotonic()
  try:
    return await retry_async(request, _POLICY, label='LLM provider')
  except Exception as error:
    if not _transient(error):
      raise
    elapsed = monotonic() - started
    raise LLMUnavailable(f'LLM provider unavailable for {elapsed:.0f} s: {error}') from error
