import subprocess
import textwrap
from pathlib import Path

import pytest

from bro.dev.sleep_policy import Violation, assert_sleep_policy, violations


def _violations(source: str) -> list[Violation]:
  return violations(textwrap.dedent(source))


def _git_repo(path: Path) -> None:
  subprocess.run(['git', 'init', '-q', str(path)], check=True)


def test_a_yield_is_admitted():
  assert (
    _violations(
      """
      async def test_it():
        await asyncio.sleep(0)
        time.sleep(0.0)
      """
    )
    == []
  )


def test_a_bare_sleep_is_a_timer_wait():
  assert _violations(
    """
    def test_it():
      time.sleep(0.1)
    """
  ) == [Violation(3, 'a timer wait')]


def test_a_sleep_reached_through_any_name_is_seen():
  assert _violations(
    """
    from time import sleep as pause

    nap = time.sleep


    async def test_it():
      await __import__('asyncio').sleep(0.1)
      sleep(0.1)
      pause(0.1)
      nap(0.1)
    """
  ) == [Violation(line, 'a timer wait') for line in (8, 9, 10, 11)]


@pytest.mark.parametrize(
  'source',
  [
    """
    def test_it():
      for _ in range(3):
        time.sleep(0.5)
      assert done()
    """,
    """
    def test_it():
      while True:
        time.sleep(0.1)
      raise AssertionError('unreachable')
    """,
    """
    def test_it():
      deadline = time.monotonic() + 5
      while time.monotonic() < deadline:
        time.sleep(0.1)
      raise AssertionError('never ready')
    """,
  ],
)
def test_a_loop_that_checks_no_signal_is_a_timer_wait(source):
  assert [violation.reason for violation in _violations(source)] == ['a timer wait']


@pytest.mark.parametrize(
  'source',
  [
    """
    async def test_it():
      async with asyncio.timeout(5):
        while not ready():
          await asyncio.sleep(0.01)
    """,
    """
    def test_it():
      deadline = time.monotonic() + 5
      while not ready():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    """,
    """
    def test_it():
      deadline = time.monotonic() + 5
      while True:
        if ready():
          break
        if time.monotonic() >= deadline:
          pytest.fail('never ready')
        time.sleep(0.01)
    """,
    """
    def test_it():
      deadline = time.time() + 5
      while time.time() < deadline:
        if ready():
          return
        time.sleep(0.01)
      raise AssertionError('never ready')
    """,
    """
    def test_it():
      for _ in range(100):
        if ready():
          break
        time.sleep(0.01)
      else:
        raise AssertionError('never ready')
    """,
    """
    def test_it():
      for _ in range(100):
        if ready():
          return
        time.sleep(0.01)
      pytest.fail('never ready')
    """,
  ],
)
def test_a_poll_interval_in_a_loop_bounded_by_a_deadline_is_admitted(source):
  assert _violations(source) == []


@pytest.mark.parametrize(
  'source',
  [
    """
    def test_it():
      for _ in range(100):
        if ready():
          break
        time.sleep(0.01)
      assert_state()
    """,
    """
    def test_it():
      while not ready():
        time.sleep(0.01)
    """,
    """
    def test_it():
      while not ready():
        assert service.is_alive()
        time.sleep(0.1)
    """,
    """
    def test_it():
      for _ in itertools.count():
        if ready():
          return
        time.sleep(0.1)
      pytest.fail('never ready')
    """,
    """
    def test_it():
      for attempt in attempts:
        if ready():
          break
        time.sleep(0.1)
      else:
        raise AssertionError('never ready')
    """,
    """
    def test_it():
      deadline = time.monotonic() + 5
      while not ready():
        while not settled():
          time.sleep(0.01)
        assert time.monotonic() < deadline
    """,
    """
    async def test_it():
      async with asyncio.timeout(5):

        async def poll():
          while not ready():
            await asyncio.sleep(0.01)

        await poll()
    """,
  ],
)
def test_a_poll_interval_in_a_loop_without_a_deadline_is_rejected(source):
  assert [violation.reason for violation in _violations(source)] == [
    'a poll interval in a loop without a loud bound'
  ]


def test_a_marker_admits_a_subject_or_bound_sleep():
  assert (
    _violations(
      """
      def test_it():
        time.sleep(0.002)  # sleep: subject — crosses a millisecond boundary
        time.sleep(0.5)  # sleep: bound
      """
    )
    == []
  )


def test_a_marker_on_any_line_of_the_call_counts():
  assert (
    _violations(
      """
      async def test_it():
        await asyncio.sleep(
          0.5,  # sleep: bound
        )
      """
    )
    == []
  )


def test_a_marker_on_an_admitted_sleep_is_rejected():
  assert _violations(
    """
    async def test_it():
      await asyncio.sleep(0)  # sleep: subject
      async with asyncio.timeout(5):
        while not ready():
          await asyncio.sleep(0.01)  # sleep: bound
    """
  ) == [
    Violation(3, '`# sleep: subject` on a yield'),
    Violation(6, '`# sleep: bound` on a poll interval'),
  ]


def test_an_unknown_class_is_rejected():
  assert _violations(
    """
    def test_it():
      time.sleep(0.1)  # sleep: payload
    """
  ) == [Violation(3, "unknown sleep class 'payload'")]


def test_more_than_one_marker_on_a_call_is_rejected():
  assert _violations(
    """
    async def test_it():
      await asyncio.sleep(
        0.5,  # sleep: bound
      )  # sleep: payload
      time.sleep(0.1)  # sleep: bound, sleep: subject
    """
  ) == [
    Violation(3, '2 sleep markers on one call'),
    Violation(6, '2 sleep markers on one call'),
  ]


def test_a_marker_with_no_sleep_is_rejected():
  assert _violations(
    """
    def test_it():
      # sleep: bound
      wait()
    """
  ) == [Violation(3, 'a sleep marker with no sleep')]


def test_a_sleep_inside_a_child_script_is_outside_the_policy():
  assert (
    _violations(
      """
      def test_it():
        run([sys.executable, '-c', 'import time; time.sleep(30)'])
      """
    )
    == []
  )


def test_the_repository_check_names_each_offending_test_module(tmp_path):
  (tmp_path / 'thing_test.py').write_text('def test_it():\n  time.sleep(0.1)\n')
  (tmp_path / 'conftest.py').write_text('def settle():\n  time.sleep(0.1)  # sleep: bound\n')
  (tmp_path / 'thing.py').write_text('def wait():\n  time.sleep(0.1)\n')
  _git_repo(tmp_path)

  with pytest.raises(AssertionError) as failure:
    assert_sleep_policy(tmp_path)

  assert str(failure.value).splitlines()[1:] == ['thing_test.py:2: a timer wait']


def test_the_repository_check_needs_test_modules(tmp_path):
  (tmp_path / 'thing.py').write_text('def wait():\n  time.sleep(0.1)\n')
  _git_repo(tmp_path)

  with pytest.raises(AssertionError, match='no test modules'):
    assert_sleep_policy(tmp_path)
