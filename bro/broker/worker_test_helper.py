"""a scheduler that records the timers it arms, for the tests that strike a worker's deadline by hand."""

from collections.abc import Callable


class FakeTimer:
  def __init__(self, seconds: float, callback: Callable[[], None]):
    self.seconds = seconds
    self.cancelled = False
    self._callback = callback

  def cancel(self) -> None:
    self.cancelled = True

  def fire(self) -> None:
    if self.cancelled:
      raise RuntimeError('fired a cancelled timer')
    self._callback()


class FakeScheduler:
  def __init__(self):
    self.timers: list[FakeTimer] = []

  def __call__(self, seconds: float, callback: Callable[[], None]) -> FakeTimer:
    timer = FakeTimer(seconds, callback)
    self.timers.append(timer)
    return timer
