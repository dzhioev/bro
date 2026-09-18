import threading
import time
from typing import Any, cast

import pytest

from bro.base.text_window import BYTE_LIMIT
from bro.inbox import Inbox
from bro.jobs import Job, Registry


def _wait_finished(job: Job) -> None:
  deadline = time.monotonic() + 10
  with job._condition:
    while not job._finished_locked():
      remaining = deadline - time.monotonic()
      assert remaining > 0, 'job did not finish'
      job._condition.wait(remaining)


def test_wait_wakes_without_consuming_news():
  inbox = Inbox()
  job = Job('job-1', 'printf news', 'watch', inbox=inbox)
  cancelled = threading.Event()

  assert inbox.wait(time.monotonic() + 10, cancelled)
  assert inbox.has_news()
  batch = inbox.drain()

  assert batch is not None
  assert batch.job_ids == ('job-1',)
  assert 'printf news' in batch.text
  assert batch.text.endswith('news')
  job.kill()


def test_foreground_exit_wakes_the_shared_condition_without_reporting_output():
  inbox = Inbox()
  job = Job('job-1', 'sleep 0.1; echo done', 'fg', inbox=inbox)

  assert inbox.wait(time.monotonic() + 10, threading.Event())
  assert job.foreground_result() == 'exited (code 0)\ndone'
  assert inbox.drain() is None


def test_wait_ends_through_its_own_cancel_event():
  inbox = Inbox()
  cancelled = threading.Event()
  result: list[bool] = []
  waiter = threading.Thread(target=lambda: result.append(inbox.wait(None, cancelled)))
  waiter.start()

  inbox.cancel(cancelled)
  waiter.join(timeout=10)

  assert not waiter.is_alive()
  assert result == [False]


def test_watch_drain_pages_oldest_output_and_keeps_remainder_pending():
  inbox = Inbox()
  job = Job('job-1', 'seq 1 105', 'watch', inbox=inbox)
  _wait_finished(job)

  first = inbox.drain(limit=100)
  second = inbox.drain(limit=100)

  assert first is not None and second is not None
  assert '[job-1 watch `seq 1 105` exited (code 0)]' in first.text
  assert '[...pending: 5 lines' in first.text
  assert 'poll job-1' in first.text
  assert second.text.endswith('\n101\n102\n103\n104\n105')
  assert inbox.drain() is None


def test_background_exit_tail_jumps_the_cursor_and_is_consumed_once():
  inbox = Inbox()
  job = Job('job-1', 'seq 1 20; exit 3', 'bg', inbox=inbox)
  _wait_finished(job)

  batch = inbox.drain(limit=5)

  assert batch is not None
  assert '[job-1 bg `seq 1 20; exit 3` exited (code 3)]' in batch.text
  assert '[...skipped before: 15 lines' in batch.text
  assert batch.text.endswith('\n16\n17\n18\n19\n20')
  assert inbox.drain() is None


def test_live_foreground_job_becomes_background_and_reports_its_exit():
  inbox = Inbox()
  job = Job('job-1', 'sleep 0.1; echo done', 'fg', inbox=inbox)

  job.become_background()
  assert inbox.wait(time.monotonic() + 10, threading.Event())
  batch = inbox.drain()

  assert batch is not None
  assert '[job-1 bg `sleep 0.1; echo done` exited (code 0)]' in batch.text
  assert batch.text.endswith('\ndone')


def test_kill_consumes_a_background_exit_before_the_inbox_can_deliver_it():
  inbox = Inbox()
  job = Job('job-1', 'sleep 30', 'bg', inbox=inbox)

  assert job.kill() == 'job-1 exited (code -15)'
  assert inbox.drain() is None


def test_foreground_result_wins_the_exit_race_against_the_drain():
  inbox = Inbox()
  job = Job('job-1', 'echo done', 'fg', inbox=inbox)
  _wait_finished(job)

  assert job.foreground_result() == 'exited (code 0)\ndone'
  assert inbox.drain() is None
  job.become_background()
  assert inbox.drain() is None


def test_drain_can_win_the_foreground_exit_race_once():
  inbox = Inbox()
  job = Job('job-1', 'echo done', 'fg', inbox=inbox)
  _wait_finished(job)

  batch = inbox.drain()

  assert batch is not None
  assert '[job-1 fg `echo done` exited (code 0)]' in batch.text
  with pytest.raises(RuntimeError, match='already consumed'):
    job.foreground_result()


def test_spool_rolls_to_disk_past_its_memory_threshold():
  job = Job('job-1', f'head -c {BYTE_LIMIT} /dev/zero', spool_memory_bytes=100)
  _wait_finished(job)

  assert cast(Any, job._spool)._rolled is True


def test_registry_close_group_kills_live_descendants():
  registry = Registry()
  job = registry.start('sleep 30 & wait')

  registry.close()

  assert job.process.wait(timeout=10) == -9
