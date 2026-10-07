"""The per-run wake condition and background-job notification drain."""

import contextlib
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Optional

from bro.base.text_window import DEFAULT_LIMIT
from bro.jobs import Job, Notification

_OPENING_LINE = (
  "[notification: this run's background jobs reported; the lines below are their output]"
)


@dataclass(frozen=True)
class NotificationBatch:
  text: str
  job_ids: tuple[str, ...]


class Inbox:
  """Wake waiters on job news and framework notices without consuming them."""

  def __init__(self):
    self._condition = threading.Condition()
    self._jobs_with_news: set[Job] = set()
    self._notices: list[tuple[int, str]] = []
    self._next_notice_id = 0
    self._last_drained_notice_id = 0

  def mark(self, job: Job) -> None:
    with self._condition:
      self._jobs_with_news.add(job)
      self._condition.notify_all()

  def post(self, notice: str) -> int:
    """Queue a framework notice and return its delivery id."""
    with self._condition:
      self._next_notice_id += 1
      notice_id = self._next_notice_id
      self._notices.append((notice_id, notice))
      self._condition.notify_all()
      return notice_id

  def wait_until_drained(self, notice_id: int, cancelled: threading.Event) -> bool:
    """Wait until a drain takes the notice, or until the caller cancels."""
    with self._condition:
      while self._last_drained_notice_id < notice_id and not cancelled.is_set():
        self._condition.wait()
      return self._last_drained_notice_id >= notice_id

  def notify(self) -> None:
    with self._condition:
      self._condition.notify_all()

  def cancel(self, cancelled: threading.Event) -> None:
    cancelled.set()
    self.notify()

  @contextlib.contextmanager
  def waiter(self) -> Generator[threading.Event]:
    cancelled = threading.Event()
    try:
      yield cancelled
    finally:
      self.cancel(cancelled)

  def _has_news_locked(self) -> bool:
    self._jobs_with_news = {job for job in self._jobs_with_news if job.has_news()}
    return len(self._jobs_with_news) > 0 or len(self._notices) > 0

  def wait(self, deadline: Optional[float], cancelled: threading.Event) -> bool:
    """Return on news, cancellation, or the monotonic deadline; consume nothing."""
    with self._condition:
      while not self._has_news_locked() and not cancelled.is_set():
        if deadline is None:
          self._condition.wait()
        else:
          remaining = deadline - time.monotonic()
          if remaining <= 0:
            return False
          self._condition.wait(remaining)
      return self._has_news_locked()

  def has_news(self) -> bool:
    with self._condition:
      return self._has_news_locked()

  def drain(self, limit: int = DEFAULT_LIMIT) -> Optional[NotificationBatch]:
    with self._condition:
      jobs = sorted(self._jobs_with_news, key=lambda job: job.id)
      self._jobs_with_news.clear()
      notices = self._notices
      self._notices = []
      if len(notices) > 0:
        self._last_drained_notice_id = notices[-1][0]
        self._condition.notify_all()

    notifications: list[Notification] = []
    for job in jobs:
      notification = job.drain_notification(limit)
      if notification is not None:
        notifications.append(notification)
      if job.has_news():
        self.mark(job)

    if len(notifications) == 0 and len(notices) == 0:
      return None
    parts = [text for _, text in notices]
    if len(notifications) > 0:
      parts.insert(
        0, '\n'.join([_OPENING_LINE, *(_format(notification) for notification in notifications)])
      )
    return NotificationBatch(
      text='\n'.join(parts),
      job_ids=tuple(notification.job_id for notification in notifications),
    )


def _format(notification: Notification) -> str:
  command = notification.command.replace('`', '\\`')
  header = f'[{notification.job_id} {notification.mode} `{command}` exited (code {notification.exit_code})]'
  return header if len(notification.lines) == 0 else f'{header}\n{notification.lines}'
