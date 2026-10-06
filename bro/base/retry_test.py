import asyncio

import pytest

from bro.base import retry


class _Clock:
  def __init__(self) -> None:
    self.now = 0.0
    self.waits: list[float] = []

  def monotonic(self) -> float:
    return self.now

  def pause(self, seconds: float) -> None:
    self.waits.append(seconds)
    self.now += seconds

  async def pause_async(self, seconds: float) -> None:
    self.pause(seconds)


class _Blip(Exception):
  pass


@pytest.fixture
def clock(monkeypatch) -> _Clock:
  clock = _Clock()
  monkeypatch.setattr(retry, 'monotonic', clock.monotonic)
  monkeypatch.setattr(retry, 'sleep', clock.pause)
  monkeypatch.setattr(retry, 'asyncio_sleep', clock.pause_async)
  return clock


def _failing(clock: _Clock, failures: int, error: Exception, attempt_seconds: float = 0.0):
  calls = []

  def call() -> str:
    calls.append(clock.now)
    clock.now += attempt_seconds
    if len(calls) <= failures:
      raise error
    return 'done'

  return call, calls


def _policy(**limits) -> retry.RetryPolicy:
  return retry.RetryPolicy(
    retryable=lambda error: isinstance(error, _Blip),
    delay=lambda error, attempt: float(attempt + 1),
    **limits,
  )


def test_a_retryable_failure_is_repeated_after_the_policy_delay(clock) -> None:
  call, calls = _failing(clock, 2, _Blip('blip'))

  assert retry.retry(call, _policy(attempts=3)) == 'done'
  assert len(calls) == 3
  assert clock.waits == [1.0, 2.0]


def test_the_last_failure_propagates_once_attempts_run_out(clock) -> None:
  call, calls = _failing(clock, 5, _Blip('blip'))

  with pytest.raises(_Blip):
    retry.retry(call, _policy(attempts=3))
  assert len(calls) == 3


def test_no_attempt_starts_past_the_window(clock) -> None:
  call, calls = _failing(clock, 100, _Blip('blip'), attempt_seconds=10.0)

  with pytest.raises(_Blip):
    retry.retry(call, _policy(window=60.0))
  assert len(calls) > 1
  assert calls[-1] < 60.0


def test_a_failure_the_policy_does_not_retry_propagates_at_once(clock) -> None:
  call, calls = _failing(clock, 1, ValueError('bad request'))

  with pytest.raises(ValueError):
    retry.retry(call, _policy(attempts=5))
  assert len(calls) == 1
  assert clock.waits == []


def test_the_awaitable_loop_follows_the_same_policy(clock) -> None:
  call, calls = _failing(clock, 1, _Blip('blip'))

  async def call_async() -> str:
    return call()

  assert asyncio.run(retry.retry_async(call_async, _policy(attempts=2))) == 'done'
  assert len(calls) == 2


def test_exponential_backoff_doubles_up_to_its_cap() -> None:
  delay = retry.exponential_backoff(1.0, 5.0)

  assert [delay(_Blip(), attempt) for attempt in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_a_policy_without_any_limit_is_refused() -> None:
  with pytest.raises(ValueError, match='attempt count, a window'):
    _policy()
