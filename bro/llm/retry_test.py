import asyncio

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from bro.base import retry
from bro.llm import retry as llm_retry
from bro.llm.llm import LLMUnavailable
from bro.llm.llms.openai import LLMSpec

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
  monkeypatch.setattr(llm_retry, 'monotonic', clock.monotonic)
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

  assert asyncio.run(llm_retry.retry_llm(request, LLMSpec().is_transient)) == 'reply'
  assert len(attempts) == 3


@pytest.mark.parametrize('status', [400, 429])
def test_client_errors_and_rate_limits_are_not_repeated(clock, status) -> None:
  request, attempts = _request(clock, 1, _status_error(status))

  with pytest.raises(APIStatusError):
    asyncio.run(llm_retry.retry_llm(request, LLMSpec().is_transient))
  assert len(attempts) == 1


def test_an_outage_past_the_window_raises_the_neutral_error(clock) -> None:
  request, attempts = _request(clock, 1000, APIConnectionError(request=_REQUEST))

  with pytest.raises(LLMUnavailable) as error:
    asyncio.run(llm_retry.retry_llm(request, LLMSpec().is_transient))

  assert len(attempts) > 1
  assert attempts[-1] < llm_retry.RETRY_WINDOW_SECONDS
  assert isinstance(error.value.__cause__, APIConnectionError)
  assert 'openai' not in str(error.value).lower()


@pytest.mark.parametrize('refusals, expected_wait', [(3, 70), (100, 610)])
def test_capacity_retry_counts_each_attempt_and_uses_the_bounded_schedule(
  clock, refusals, expected_wait, monkeypatch
):
  attempted = []
  recorded = []
  waits = []

  async def pause(seconds):
    waits.append(seconds)
    await clock.pause(seconds)

  monkeypatch.setattr(llm_retry, 'asyncio_sleep', pause, raising=False)

  async def request():
    attempted.append(clock.now)
    return 'capacity' if len(attempted) <= refusals else 'completed'

  async def record(leg):
    recorded.append(leg)

  result = asyncio.run(
    llm_retry.retry_llm(
      request, lambda error: False, capacity=lambda leg: leg == 'capacity', on_capacity=record
    )
  )
  assert result == ('completed' if refusals == 3 else 'capacity')
  assert waits == ([10, 20, 40] if refusals == 3 else [10, 20, 40, 80, 160, 300])
  assert clock.now == expected_wait
  assert len(attempted) == (4 if refusals == 3 else 7)
  assert recorded == ['capacity'] * (len(attempted) - 1)


@pytest.mark.asyncio
async def test_capacity_wait_is_cancelled_without_another_attempt(monkeypatch):
  waiting = asyncio.Event()
  attempted = []
  recorded = []

  async def pause(seconds):
    waiting.set()
    await asyncio.Future()

  monkeypatch.setattr(llm_retry, 'asyncio_sleep', pause, raising=False)

  async def request():
    attempted.append('request')
    return 'capacity'

  async def record(leg):
    recorded.append(leg)

  task = asyncio.create_task(
    llm_retry.retry_llm(
      request, lambda error: False, capacity=lambda leg: leg == 'capacity', on_capacity=record
    )
  )
  await waiting.wait()
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert attempted == ['request']
  assert recorded == ['capacity']


@pytest.mark.asyncio
async def test_capacity_schedule_restarts_and_other_stops_return_immediately(clock, monkeypatch):
  monkeypatch.setattr(llm_retry, 'asyncio_sleep', clock.pause, raising=False)
  sequence = iter(['capacity', 'completed', 'capacity', 'filtered'])

  async def request():
    return next(sequence)

  for expected in ('completed', 'filtered'):
    assert (
      await llm_retry.retry_llm(
        request, lambda error: False, capacity=lambda leg: leg == 'capacity'
      )
      == expected
    )
  assert clock.now == 20
