import asyncio

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from bro.base import retry
from bro.llm import openai_retry
from bro.llm.llm import LLMUnavailable

_REQUEST = httpx2.Request('POST', 'https://api.example.test/v1/responses')


class _Clock:
  def __init__(self) -> None:
    self.now = 0.0

  def monotonic(self) -> float:
    return self.now

  async def pause(self, seconds: float) -> None:
    self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
  clock = _Clock()
  monkeypatch.setattr(retry, 'monotonic', clock.monotonic)
  monkeypatch.setattr(retry, 'asyncio_sleep', clock.pause)
  monkeypatch.setattr(openai_retry, 'monotonic', clock.monotonic)
  return clock


def _status_error(status: int) -> APIStatusError:
  return APIStatusError(
    f'HTTP {status}', response=httpx2.Response(status, request=_REQUEST), body=None
  )


def _request(clock: _Clock, failures: int, error: Exception, attempt_seconds: float = 16.0):
  attempts = []

  async def request() -> str:
    attempts.append(clock.now)
    clock.now += attempt_seconds
    if len(attempts) <= failures:
      raise error
    return 'reply'

  return request, attempts


@pytest.mark.parametrize(
  'error',
  [
    APIConnectionError(request=_REQUEST),
    APITimeoutError(request=_REQUEST),
    _status_error(500),
    _status_error(503),
  ],
  ids=['connection', 'timeout', 'internal-error', 'unavailable'],
)
def test_transient_failures_are_repeated(clock, error) -> None:
  request, attempts = _request(clock, 2, error)

  assert asyncio.run(openai_retry.retry_openai(request)) == 'reply'
  assert len(attempts) == 3


@pytest.mark.parametrize('status', [400, 429])
def test_client_errors_and_rate_limits_are_not_repeated(clock, status) -> None:
  request, attempts = _request(clock, 1, _status_error(status))

  with pytest.raises(APIStatusError):
    asyncio.run(openai_retry.retry_openai(request))
  assert len(attempts) == 1


def test_an_outage_past_the_window_raises_the_neutral_error(clock) -> None:
  request, attempts = _request(clock, 1000, APIConnectionError(request=_REQUEST))

  with pytest.raises(LLMUnavailable) as error:
    asyncio.run(openai_retry.retry_openai(request))

  assert len(attempts) > 1
  assert attempts[-1] < openai_retry.RETRY_WINDOW_SECONDS
  assert isinstance(error.value.__cause__, APIConnectionError)
  assert 'openai' not in str(error.value).lower()
