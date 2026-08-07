import threading
import time

import pytest

from bro.bros.dev.jobs import Job, Registry


def _wait_finished(job: Job, timeout: float = 10.0) -> None:
  """block until the job has exited and its spool is fully drained, without
  touching the watch cursor (tests want a deterministic backlog before the
  first read)."""
  deadline = time.monotonic() + timeout
  with job._condition:
    while not job._finished():
      remaining = deadline - time.monotonic()
      assert remaining > 0, 'job did not finish in time'
      job._condition.wait(remaining)


def _await_spool(job: Job, expected: str, timeout: float = 10.0) -> None:
  deadline = time.monotonic() + timeout
  with job._condition:
    while expected not in job._spool.getvalue():
      remaining = deadline - time.monotonic()
      assert remaining > 0, f'{expected!r} never spooled'
      job._condition.wait(remaining)


def _drain(job: Job, limit: int) -> list[str]:
  """watch until a bare exited state line; returns every watch result."""
  results = []
  for _ in range(300):
    out = job.watch(wait_seconds=5, limit=limit, tail=False)
    results.append(out)
    if out.startswith('exited') and '\n' not in out:
      return results
  raise AssertionError('job never drained')


def _body(watch_result: str) -> list[str]:
  # strip the state line and any markers, keep the output lines
  lines = watch_result.splitlines()[1:]
  return [line for line in lines if not line.startswith('[...')]


def test_registry_ids_increment_and_get_resolves():
  registry = Registry()
  first = registry.start('true')
  second = registry.start('true')
  assert first.id == 'job-1'
  assert second.id == 'job-2'
  assert registry.get('job-1') is first
  first.process.wait()
  second.process.wait()


def test_registry_unknown_id_lists_known_jobs():
  registry = Registry()
  registry.start('true').process.wait()
  with pytest.raises(ValueError, match=r"unknown job id 'job-9'; known jobs: job-1"):
    registry.get('job-9')


def test_watch_tail_waits_for_exit_and_returns_output():
  job = Job('job-1', 'echo one; echo two >&2; exit 3')
  out = job.watch(wait_seconds=10, limit=100, tail=True)
  assert out.startswith('exited (code 3)\n')
  # stderr merged into the one chronological stream
  assert 'one\ntwo' in out
  # cursor jumped to the spool end: the next incremental watch is a bare state line
  assert job.watch(wait_seconds=0, limit=100, tail=False) == 'exited (code 3)'


def test_watch_incremental_paginates_oldest_first_with_pending_marker():
  total = 10
  job = Job('job-1', f'seq 1 {total}')
  _wait_finished(job)
  first = job.watch(wait_seconds=0, limit=3, tail=False)
  assert first.startswith('exited (code 0)\n')
  assert _body(first) == ['1', '2', '3']
  assert '[...pending: 7 lines' in first
  second = job.watch(wait_seconds=0, limit=3, tail=False)
  assert _body(second) == ['4', '5', '6']
  results = _drain(job, limit=3)
  collected = [line for result in results for line in _body(result)]
  assert collected == [str(i) for i in range(7, total + 1)]
  # drained + exited: the terminal result is the bare state line
  assert results[-1] == 'exited (code 0)'


def test_watch_incremental_loses_nothing_across_slices():
  total = 50
  job = Job('job-1', f'seq 1 {total}')
  results = _drain(job, limit=7)
  collected = [line for result in results for line in _body(result)]
  assert collected == [str(i) for i in range(1, total + 1)]


def test_watch_quiet_window_returns_bare_running_heartbeat():
  job = Job('job-1', 'sleep 30')
  started = time.monotonic()
  assert job.watch(wait_seconds=0.2, limit=100, tail=False) == 'running'
  assert time.monotonic() - started < 5
  job.kill(grace_seconds=1)


def test_watch_nonblocking_poll_returns_immediately():
  job = Job('job-1', 'sleep 30')
  started = time.monotonic()
  assert job.watch(wait_seconds=0, limit=100, tail=False) == 'running'
  assert time.monotonic() - started < 1
  job.kill(grace_seconds=1)


def test_watch_blocks_until_output_arrives():
  job = Job('job-1', 'sleep 0.3; echo late')
  out = job.watch(wait_seconds=10, limit=100, tail=False)
  assert _body(out) == ['late']


def test_watch_tail_timeout_gives_progress_glimpse_and_jumps_cursor():
  job = Job('job-1', 'seq 1 20; sleep 30')
  _await_spool(job, '20\n')
  out = job.watch(wait_seconds=0, limit=5, tail=True)
  assert out.startswith('running\n')
  assert 'skipped before: 15 lines' in out
  assert _body(out) == ['16', '17', '18', '19', '20']
  # the skipped middle is discarded, not pending
  assert job.watch(wait_seconds=0, limit=5, tail=False) == 'running'
  job.kill(grace_seconds=1)


def test_watch_giant_single_line_pages_mid_line_without_loss():
  length = 1000
  job = Job('job-1', f'printf "x%.0s" $(seq 1 {length}); echo')
  _wait_finished(job)
  collected = ''
  for result in _drain(job, limit=1):  # limit 1 → 150-byte budget per slice
    collected += ''.join(_body(result))
  assert collected == 'x' * length


def test_concurrent_watch_fails_immediately_and_kill_wakes_the_blocked_watch():
  job = Job('job-1', 'sleep 30')
  blocked_result: list[str] = []

  def blocked_watch():
    blocked_result.append(job.watch(wait_seconds=20, limit=100, tail=False))

  watcher = threading.Thread(target=blocked_watch)
  watcher.start()
  deadline = time.monotonic() + 5
  while not job._watch_lock.locked():
    assert time.monotonic() < deadline
    time.sleep(0.01)
  with pytest.raises(ValueError, match='job-1 is already being watched'):
    job.watch(wait_seconds=0, limit=100, tail=False)
  assert job.kill(grace_seconds=5) == 'job-1 exited (code -15)'
  watcher.join(timeout=10)
  assert not watcher.is_alive()
  assert blocked_result == ['exited (code -15)']


def test_kill_terminates_and_record_stays_readable():
  job = Job('job-1', 'echo before; sleep 30')
  _await_spool(job, 'before\n')
  assert job.kill(grace_seconds=5) == 'job-1 exited (code -15)'
  out = job.watch(wait_seconds=5, limit=100, tail=False)
  assert out.startswith('exited (code -15)\n')
  assert _body(out) == ['before']
  assert job.kill() == 'job-1 already exited (code -15)'


def test_kill_escalates_to_sigkill_when_sigterm_is_ignored():
  # exec makes the TERM-ignoring python the direct child (the group leader), so
  # the group SIGTERM is fully ignored and only the SIGKILL escalation lands.
  script = (
    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'print("ready", flush=True); time.sleep(30)'
  )
  job = Job('job-1', f"exec python3 -c '{script}'")
  _await_spool(job, 'ready\n')
  assert job.kill(grace_seconds=0.3) == 'job-1 exited (code -9)'


def test_registry_kill_running_reaps_only_live_jobs():
  registry = Registry()
  finished = registry.start('true')
  finished.process.wait()
  running = registry.start('sleep 30')
  registry.kill_running()
  assert running.process.wait(timeout=10) == -9
  assert finished.process.returncode == 0
