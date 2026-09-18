import gc
import time
import weakref
from pathlib import Path

import pytest

from bro.base.text_window import BYTE_LIMIT
from bro.jobs import Job, Registry


def _wait_finished(job: Job, timeout: float = 10.0) -> None:
  """block until the job has exited and its spool is fully drained, without
  touching the poll cursor (tests want a deterministic backlog before the
  first read)."""
  deadline = time.monotonic() + timeout
  with job._condition:
    while not job._finished_locked():
      remaining = deadline - time.monotonic()
      assert remaining > 0, 'job did not finish in time'
      job._condition.wait(remaining)


def _await_spool(job: Job, expected: str, timeout: float = 10.0) -> None:
  deadline = time.monotonic() + timeout
  with job._condition:
    while expected not in job._unread_locked():
      remaining = deadline - time.monotonic()
      assert remaining > 0, f'{expected!r} never spooled'
      job._condition.wait(remaining)


def _drain(job: Job, limit: int) -> list[str]:
  """poll until a bare exited state line; returns every poll result."""
  results = []
  for _ in range(300):
    out = job.poll(limit, tail=False)
    results.append(out)
    if out.startswith('exited') and '\n' not in out:
      return results
    time.sleep(0.01)
  raise AssertionError('job never drained')


def _body(poll_result: str) -> list[str]:
  # strip the state line and any markers, keep the output lines
  lines = poll_result.splitlines()[1:]
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


def test_poll_tail_returns_finished_output():
  job = Job('job-1', 'echo one; echo two >&2; exit 3')
  _wait_finished(job)
  out = job.poll(100, tail=True)
  assert out.startswith('exited (code 3)\n')
  # stderr merged into the one chronological stream
  assert 'one\ntwo' in out
  # cursor jumped to the spool end: the next incremental poll is a bare state line
  assert job.poll(100) == 'exited (code 3)'


def test_poll_head_paginates_oldest_first_with_pending_marker():
  total = 10
  job = Job('job-1', f'seq 1 {total}')
  _wait_finished(job)
  first = job.poll(3)
  assert first.startswith('exited (code 0)\n')
  assert _body(first) == ['1', '2', '3']
  assert '[...pending: 7 lines' in first
  second = job.poll(3)
  assert _body(second) == ['4', '5', '6']
  results = _drain(job, limit=3)
  collected = [line for result in results for line in _body(result)]
  assert collected == [str(i) for i in range(7, total + 1)]
  # drained + exited: the terminal result is the bare state line
  assert results[-1] == 'exited (code 0)'


def test_poll_head_loses_nothing_across_slices():
  total = 50
  job = Job('job-1', f'seq 1 {total}')
  results = _drain(job, limit=7)
  collected = [line for result in results for line in _body(result)]
  assert collected == [str(i) for i in range(1, total + 1)]


def test_poll_tail_gives_progress_glimpse_and_jumps_cursor():
  job = Job('job-1', 'seq 1 20; sleep 30')
  _await_spool(job, '20\n')
  out = job.poll(5, tail=True)
  assert out.startswith('running\n')
  assert 'skipped before: 15 lines' in out
  assert _body(out) == ['16', '17', '18', '19', '20']
  # the skipped middle is discarded, not pending
  assert job.poll(5) == 'running'
  job.kill(grace_seconds=1)


def test_poll_giant_single_line_pages_mid_line_without_loss():
  length = BYTE_LIMIT + 500
  job = Job('job-1', f'head -c {length} /dev/zero | tr "\\0" x; echo')
  _wait_finished(job)
  collected = ''
  for result in _drain(job, limit=1):
    collected += ''.join(_body(result))
  assert collected == 'x' * length


def test_kill_terminates_and_record_stays_readable():
  job = Job('job-1', 'echo before; sleep 30')
  _await_spool(job, 'before\n')
  killed = job.kill(grace_seconds=5)
  state = killed.removeprefix('job-1 ')
  assert state.startswith('exited (code -')
  out = job.poll(100)
  assert out.startswith(f'{state}\n')
  assert _body(out) == ['before']
  assert job.kill() == f'job-1 already {state}'


def test_command_signal_exit_is_preserved_through_the_supervisor():
  job = Job('job-1', 'kill -TERM $$', 'fg')
  _wait_finished(job)

  assert job.foreground_result() == 'exited (code -15)'


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


def _process_is_running(process_id: int) -> bool:
  try:
    state = Path(f'/proc/{process_id}/stat').read_text().split()[2]
  except (FileNotFoundError, ProcessLookupError):
    return False
  return state != 'Z'


def test_supervisor_anchors_the_group_until_nested_descendants_exit():
  job = Job('job-1', '(sleep 30 & echo $!) &')
  _await_spool(job, '\n')
  with job._condition:
    child_process_id = int(job._unread_locked())

  assert job.process.poll() is None
  assert _process_is_running(child_process_id)
  assert job.kill(grace_seconds=0.3) == 'job-1 exited (code -15)'
  assert not _process_is_running(child_process_id)


def test_registry_close_unregisters_the_atexit_backstop():
  registry = Registry()
  reference = weakref.ref(registry)

  registry.close()
  del registry
  gc.collect()

  assert reference() is None


def test_registry_close_reaps_jobs_and_releases_spools():
  registry = Registry()
  finished = registry.start('true')
  finished.process.wait()
  running = registry.start('sleep 30')
  orphaned = registry.start('(sleep 30 & echo $!) &')
  _await_spool(orphaned, '\n')
  with orphaned._condition:
    child_process_id = int(orphaned._unread_locked())

  registry.close()

  assert running.process.wait(timeout=10) == -9
  assert finished.process.returncode == 0
  assert not _process_is_running(child_process_id)
  assert all(job._spool.closed for job in (finished, running, orphaned))
  with pytest.raises(ValueError, match='unknown job id'):
    registry.get('job-1')
