import concurrent.futures
import io
import json
import os
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from bro import watches
from bro.monitor import SESSION_DIR_ENV
from ride.claude import watch_waiter
from ride.claude.claude_argv import watch_waiter_hooks
from ride.claude.waiter_state import REWAKE_STATUS, WAITER_MARK, WaiterState

_DAY_SECONDS = 24 * 60 * 60


@pytest.fixture
def owner(monkeypatch, tmp_path) -> Generator[watches.Owner]:
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / 'session'))
  with watches.Owner.for_session() as watch_owner:
    yield watch_owner


@pytest.fixture
def state(owner) -> Generator[WaiterState]:
  del owner
  waiter_state = WaiterState.for_session()
  waiter_state.reset()
  yield waiter_state
  # a waiter a failed test left polling would hold the run open at exit
  waiter_state.stand_down()


def _seed(store: watches.Store, command: str, content: str) -> None:
  watch = watches.Watch(command, store.directory, watches.slug(command))
  watch.command_file.write_text(f'{command}\n')
  watch.log.write_text(content)


class _PolledStore(watches.Store):
  """an empty store reporting when the waiter has polled it."""

  def __init__(self, directory: Path) -> None:
    super().__init__(directory, directory / 'owner')
    self.polled = threading.Event()

  def take(self) -> None:
    self.polled.set()


class _BlockingStore(watches.Store):
  """a store whose one batch is held inside `take` until released."""

  def __init__(self, directory: Path) -> None:
    super().__init__(directory, directory / 'owner')
    self.taking = threading.Event()
    self.release = threading.Event()

  def take(self) -> str:
    self.taking.set()
    assert self.release.wait(10), 'the test never released the batch'
    return '[held] line'


class _RegistrationSpy(WaiterState):
  """waiter state reporting its second registration."""

  def __init__(self, directory: Path) -> None:
    super().__init__(directory)
    self.registrations = 0
    self.second_registered = threading.Event()

  def register(self) -> str:
    token = super().register()
    self.registrations += 1
    if self.registrations == 2:
      self.second_registered.set()
    return token


def _waiting(
  state: WaiterState,
  store: watches.Store,
  *,
  runner: int | None = None,
  bound: float = _DAY_SECONDS,
) -> tuple[concurrent.futures.Future[int], io.StringIO]:
  out = io.StringIO()
  executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
  future = executor.submit(
    watch_waiter.wait, state, store, os.getpid() if runner is None else runner, bound, out
  )
  executor.shutdown(wait=False)
  return future, out


def _polling(state: WaiterState) -> tuple[concurrent.futures.Future[int], io.StringIO]:
  store = _PolledStore(state.directory / 'store')
  waiting = _waiting(state, store)
  assert store.polled.wait(10), 'the waiter never polled its store'
  return waiting


class TestWait:
  def test_a_batch_wakes_the_session_and_counts_the_rewake(self, owner, state):
    _seed(owner.store, 'printf lines', 'hello\n')

    future, out = _waiting(state, owner.store)

    assert future.result(timeout=10) == REWAKE_STATUS
    assert out.getvalue() == '\n[printf lines] hello\n'
    assert state.rewakes() == 1
    assert not owner.store.has_pending_lines()

  def test_a_superseded_waiter_stands_aside(self, state):
    future, out = _polling(state)

    state.register()

    assert future.result(timeout=10) == 0
    assert out.getvalue() == ''
    assert state.rewakes() == 0

  def test_a_waiter_registers_only_between_another_waiters_polls(self, state):
    spy = _RegistrationSpy(state.directory)
    holding = _BlockingStore(state.directory / 'holding')
    first, first_out = _waiting(spy, holding)
    assert holding.taking.wait(10), 'the first waiter never took'

    second, second_out = _waiting(spy, _PolledStore(state.directory / 'polled'))

    # a registration now would leave the first waiter delivering superseded
    assert not spy.second_registered.wait(0.5), 'the second waiter registered mid-take'
    holding.release.set()
    assert first.result(timeout=10) == REWAKE_STATUS
    assert first_out.getvalue() == '\n[held] line\n'
    assert spy.second_registered.wait(10), 'the second waiter never registered'
    state.stand_down()
    assert second.result(timeout=10) == 0
    assert second_out.getvalue() == ''

  def test_a_stood_down_waiter_stands_aside(self, state):
    future, out = _polling(state)

    state.stand_down()

    assert future.result(timeout=10) == 0
    assert out.getvalue() == ''

  def test_a_waiter_outliving_the_session_runner_stands_aside(self, owner, state):
    _seed(owner.store, 'printf lines', 'hello\n')
    runner = subprocess.Popen(['true'])
    runner.wait()

    future, out = _waiting(state, owner.store, runner=runner.pid)

    assert future.result(timeout=10) == 0
    assert out.getvalue() == ''
    assert owner.store.has_pending_lines()

  def test_a_waiter_wakes_the_session_short_of_its_bound(self, owner, state):
    future, out = _waiting(state, owner.store, bound=60)

    assert future.result(timeout=10) == REWAKE_STATUS
    assert 'ending the turn keeps waiting' in out.getvalue()
    assert state.rewakes() == 1


def _run_hook(cwd: Path) -> subprocess.CompletedProcess[str]:
  """the waiter's settings command, run through a shell as claude runs it."""
  (hook,) = watch_waiter_hooks()['Stop'][0]['hooks']
  return subprocess.run(
    hook['command'],
    shell=True,
    cwd=cwd,
    input=json.dumps({'hook_event_name': 'Stop'}),
    env={**os.environ, 'RIDE_RUNNER_PID': str(os.getpid())},
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
  )


def test_the_hook_wakes_the_session_from_its_store(owner, state, tmp_path):
  del state
  _seed(owner.store, 'quest watch', 'summon ended\n')

  completed = _run_hook(tmp_path)

  assert completed.returncode == REWAKE_STATUS, completed.stderr
  assert completed.stderr == '\n[quest watch] summon ended\n'
  assert completed.stdout.splitlines() == [WAITER_MARK]


def test_a_waiter_that_fails_to_start_still_reports_as_the_waiter(owner, state, tmp_path):
  del owner, state
  # a package in the hook's cwd shadows ride for `python -m`, as a broken
  # install would fail the waiter's import before any of its code runs
  shadow = tmp_path / 'ride'
  shadow.mkdir()
  (shadow / '__init__.py').write_text('raise ImportError("broken install")\n')

  completed = _run_hook(tmp_path)

  assert completed.returncode == 1
  assert 'broken install' in completed.stderr
  assert completed.stdout.splitlines() == [WAITER_MARK]


def test_a_reset_forgets_an_earlier_session(state):
  state.stand_down()
  with state.locked():
    state.count_rewake()

  state.reset()

  assert not state.stood_down()
  assert state.rewakes() == 0
