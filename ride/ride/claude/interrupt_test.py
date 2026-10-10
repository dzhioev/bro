import contextlib
import fcntl
import json
import os
import pty
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

import ride.claude.interrupt as interrupt
from bro import watches
from bro.monitor import SESSION_DIR_ENV
from bro.workspace import session as workspace_session
from ride.claude.fake_claude_test_helper import fake_claude_argv
from ride.claude.session_end_state import StoppedCall, StoppedCallMark
from ride.claude.waiter_state import WAITER_MARK, WaiterState

# a bash trap only runs between foreground commands, so a fake waiting for a
# signal loops over short sleeps rather than one long one
_IDLE = 'while true; do sleep 0.05; done\n'


def _fake_claude(tmp_path: Path, script: str) -> list[str]:
  fake = tmp_path / 'claude'
  fake.write_text(f'#!/usr/bin/env bash\n{script}')
  fake.chmod(0o755)
  return [str(fake)]


@contextlib.contextmanager
def _session_terminal() -> Generator[int]:
  """stand this process's stdio on a pty the way a managed session runs, and
  yield the other end — what the human in front of the session holds. Entered
  inside the test body: pytest reinstalls its own capture over these descriptors
  between phases, so a fixture's swap would not survive into the test."""
  terminal, stdio = pty.openpty()
  saved = (os.dup(0), os.dup(1))
  os.dup2(stdio, 0)
  os.dup2(stdio, 1)
  os.close(stdio)
  try:
    yield terminal
  finally:
    os.dup2(saved[0], 0)
    os.dup2(saved[1], 1)
    for descriptor in (*saved, terminal):
      os.close(descriptor)


def _read_until(terminal: int, needle: bytes, *, timeout: float = 20.0) -> bytes:
  os.set_blocking(terminal, False)
  seen = b''
  deadline = time.monotonic() + timeout
  while needle not in seen:
    assert time.monotonic() < deadline, f'{needle!r} never arrived; the terminal saw {seen!r}'
    try:
      seen += os.read(terminal, 65536)
    except BlockingIOError:
      time.sleep(0.02)
  return seen


@contextlib.contextmanager
def _session_input(data: bytes) -> Generator[None]:
  """stand this process's stdin on a pipe holding `data`, the way a session
  launched with piped input runs."""
  reader, writer = os.pipe()
  os.write(writer, data)
  os.close(writer)
  saved = os.dup(0)
  os.dup2(reader, 0)
  os.close(reader)
  try:
    yield
  finally:
    os.dup2(saved, 0)
    os.close(saved)


# what a waiter's hook event carries on stdout, as the fake's `hook(...)` argument
_WAITER_STDOUT = f'stdout={WAITER_MARK + chr(10)!r}'


@pytest.fixture
def store() -> Generator[watches.Store]:
  with watches.Owner.temporary(publish_environment=False) as owner:
    yield owner.store


@pytest.fixture
def waiters(tmp_path: Path) -> WaiterState:
  state = WaiterState(tmp_path / 'waiter')
  state.reset()
  return state


@pytest.fixture
def stopped_calls(tmp_path: Path) -> StoppedCallMark:
  return StoppedCallMark(tmp_path / 'stopped-call')


@pytest.fixture
def ended_session(tmp_path: Path, monkeypatch) -> None:
  """a session a service tool ended: the exit status it left before signaling its stop."""
  session = tmp_path / 'session'
  session.mkdir()
  monkeypatch.setenv(SESSION_DIR_ENV, str(session))
  (session / workspace_session.FILENAME).write_text('0')


def _stop_record(call_id: str) -> str:
  """claude's transcript record of the turn a `PostToolUse` hook stopped after `call_id`."""
  return json.dumps(
    {
      'type': 'attachment',
      'attachment': {'type': 'hook_stopped_continuation', 'toolUseID': call_id},
    }
  )


def _stream(
  tmp_path: Path, script: str, store: watches.Store, waiters: WaiterState, **options
) -> interrupt.StreamedRun:
  return interrupt.run_streaming(
    fake_claude_argv(tmp_path, script), os.environ, 'go', store=store, waiters=waiters, **options
  )


class TestRunStreaming:
  def test_the_prompt_is_the_first_message_and_an_idle_turn_end_ends_the_run(
    self, tmp_path, store, waiters
  ):
    run = _stream(tmp_path, 'result("echo:" + prompt)\nsys.stdin.read()\n', store, waiters)

    assert run == interrupt.StreamedRun(code=0, stopped=False, results=('echo:go',))
    assert waiters.stood_down()

  def test_a_running_task_draws_one_notice_and_holds_the_session_until_it_ends(
    self, tmp_path, store, waiters
  ):
    seen: list[str] = []
    script = (
      'tasks("t1")\nresult("started")\nresult(next_message())\n'
      'result("still waiting")\ntasks()\nresult("finished")\n'
      'result("after the end: " + repr(next_message()))\n'
    )

    run = _stream(tmp_path, script, store, waiters, on_result=seen.append)

    assert (run.code, run.stopped) == (0, False)
    started, notice, waiting, finished, after_end = run.results
    assert (started, waiting, finished) == ('started', 'still waiting', 'finished')
    assert 't1 local_bash `work t1`' in notice
    assert after_end == 'after the end: None'
    assert seen == list(run.results)

  def test_ambient_tasks_are_no_background_work(self, tmp_path, store, waiters):
    script = 'tasks(ambient=("housekeeping",))\nresult("idle")\nresult(repr(next_message()))\n'

    run = _stream(tmp_path, script, store, waiters)

    assert run.results == ('idle', 'None')

  def test_a_live_watch_holds_the_session_without_a_notice(self, tmp_path, store, waiters):
    store.start('sleep 30')
    script = (
      'result("waiting")\n'
      'signal.signal(signal.SIGALRM, lambda *_: result("no message"))\n'
      'signal.setitimer(signal.ITIMER_REAL, 1.0)\n'
      'result(repr(next_message()))\n'
    )

    def _stop_the_watch(reply: str) -> None:
      if reply == 'no message':
        store.stop('sleep 30')

    run = _stream(tmp_path, script, store, waiters, on_result=_stop_the_watch)

    assert run.results == ('waiting', 'no message', 'None')

  def test_a_rewake_begun_but_unseen_defers_the_turn_end_to_the_turn_it_starts(
    self, tmp_path, store, waiters
  ):
    with waiters.locked():
      waiters.record_rewake(watches.Batch((watches.BatchLine(None, '[w] line', True, 0.0),)))
    script = (
      f'tasks("t1")\nresult("first")\nhook("Stop", 2, "[w] line", {_WAITER_STDOUT})\ntasks()\n'
      'result("rewoken")\nresult("stdin: " + repr(next_message()))\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    assert run.results == ('first', 'rewoken', 'stdin: None')

  def test_without_a_rewake_in_flight_the_same_turn_end_is_settled(self, tmp_path, store, waiters):
    script = (
      f'tasks("t1")\nresult("first")\nhook("Stop", 2, "[w] line", {_WAITER_STDOUT})\ntasks()\n'
      'result("rewoken")\nresult("stdin: " + repr(next_message()))\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    first, rewoken, stdin = run.results
    assert (first, rewoken) == ('first', 'rewoken')
    assert 't1 local_bash' in stdin

  def test_another_stop_hooks_exit_is_no_rewake_of_the_waiter(self, tmp_path, store, waiters):
    with waiters.locked():
      waiters.record_rewake(watches.Batch((watches.BatchLine(None, '[w] line', True, 0.0),)))
    script = (
      'tasks("t1")\nresult("first")\nhook("Stop", 2, "project feedback")\nresult("second")\n'
      f'hook("Stop", 2, "[w] line", {_WAITER_STDOUT})\ntasks()\nresult("rewoken")\n'
      'result("stdin: " + repr(next_message()))\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    assert run.results == ('first', 'second', 'rewoken', 'stdin: None')

  def test_a_failed_waiter_fails_the_run(self, tmp_path, store, waiters):
    script = f'hook("StopFailure", 1, "Traceback: boom", {_WAITER_STDOUT})\nsys.stdin.read()\n'

    with pytest.raises(RuntimeError, match='the watch waiter failed with 1: Traceback: boom'):
      _stream(tmp_path, script, store, waiters)

  def test_another_stop_failure_hooks_failure_leaves_the_run_alone(self, tmp_path, store, waiters):
    script = 'hook("StopFailure", 1, "project hook failed")\nresult("done")\nsys.stdin.read()\n'

    run = _stream(tmp_path, script, store, waiters)

    assert run == interrupt.StreamedRun(code=0, stopped=False, results=('done',))

  def test_a_stop_stands_the_waiter_down_then_arrives_as_the_interrupt(
    self, tmp_path, store, waiters, monkeypatch
  ):
    order: list[object] = []
    stand_down = WaiterState.stand_down

    def _stand_down(state: WaiterState) -> None:
      order.append('stand down')
      stand_down(state)

    send_signal = subprocess.Popen.send_signal

    def _send_signal(process: subprocess.Popen, signal_number: int) -> None:
      order.append(signal_number)
      send_signal(process, signal_number)

    monkeypatch.setattr(WaiterState, 'stand_down', _stand_down)
    monkeypatch.setattr(subprocess.Popen, 'send_signal', _send_signal)
    script = (
      'signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\ntasks("t1")\nresult("started")\n'
      'next_message()\nos.kill(os.getppid(), signal.SIGTERM)\ntime.sleep(10)\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    assert run.stopped and run.code == 0
    assert order[:2] == ['stand down', signal.SIGINT]

  def test_a_claude_deaf_to_the_interrupt_is_terminated(
    self, tmp_path, store, waiters, monkeypatch
  ):
    monkeypatch.setattr(interrupt, '_EXIT_TIMEOUT_SECONDS', 0.2)
    script = (
      'signal.signal(signal.SIGINT, signal.SIG_IGN)\ntasks("t1")\nresult("started")\n'
      'next_message()\nos.kill(os.getppid(), signal.SIGTERM)\ntime.sleep(10)\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    assert (run.code, run.stopped) == (-signal.SIGTERM, True)

  def test_a_session_that_ended_itself_ends_at_its_stopped_turn_uninterrupted(
    self, tmp_path, store, waiters, ended_session
  ):
    script = (
      'signal.signal(signal.SIGINT, lambda *_: sys.exit(9))\ntasks("t1")\n'
      'os.kill(os.getppid(), signal.SIGTERM)\nresult("stopped")\n'
      'result("stdin: " + repr(next_message()))\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    assert run == interrupt.StreamedRun(code=0, stopped=True, results=('stopped', 'stdin: None'))
    assert waiters.stood_down()

  def test_a_session_that_ended_itself_is_interrupted_when_its_turn_never_ends(
    self, tmp_path, store, waiters, ended_session, monkeypatch
  ):
    monkeypatch.setattr(interrupt, '_STOPPED_TURN_TIMEOUT_SECONDS', 0.2)
    script = (
      'signal.signal(signal.SIGINT, lambda *_: sys.exit(9))\n'
      'os.kill(os.getppid(), signal.SIGTERM)\ntime.sleep(10)\n'
    )

    run = _stream(tmp_path, script, store, waiters)

    assert (run.code, run.stopped) == (9, True)

  def test_a_line_that_is_not_stream_json_fails_the_run(self, tmp_path, store, waiters):
    script = 'print("garbage")\nsys.stdout.flush()\nsys.stdin.read()\n'
    with pytest.raises(RuntimeError, match='not stream-json'):
      _stream(tmp_path, script, store, waiters)

  def test_a_malformed_task_list_fails_the_run(self, tmp_path, store, waiters):
    script = (
      'emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "t1"}])\n'
      'sys.stdin.read()\n'
    )
    with pytest.raises(RuntimeError, match='malformed task list'):
      _stream(tmp_path, script, store, waiters)


def _interactive(
  argv: list[str], tmp_path: Path, waiters: WaiterState, stopped_calls: StoppedCallMark
) -> interrupt.Run:
  return interrupt.run_interactive(
    argv, os.environ, tmp_path / 'projects', waiters=waiters, stopped_calls=stopped_calls
  )


class TestRunInteractive:
  def test_a_stop_arrives_as_the_interrupt_keypress_then_quits(
    self, tmp_path, waiters, stopped_calls, monkeypatch
  ):
    monkeypatch.setattr(interrupt, '_FLUSH_SETTLE_SECONDS', 0.05)
    seen_key = tmp_path / 'seen-key'
    argv = _fake_claude(
      tmp_path,
      # raw mode is what makes Ctrl-C a keypress rather than a signal, as it is
      # for claude's own TUI
      'stty raw -echo\n'
      "trap 'exit 9' INT\n"
      'sleep 0.2\n'
      'kill -TERM $PPID\n'
      "key=$(dd bs=1 count=1 2>/dev/null | od -An -tx1 | tr -d ' \\n')\n"
      f'[ "$key" = 03 ] && echo interrupted > {seen_key}\n' + _IDLE,
    )
    with _session_terminal():
      run = _interactive(argv, tmp_path, waiters, stopped_calls)
    assert (run.code, run.stopped) == (9, True)
    assert seen_key.read_text() == 'interrupted\n'
    assert waiters.stood_down()

  def test_a_zero_exit_stop_remains_distinguishable(
    self, tmp_path, waiters, stopped_calls, monkeypatch
  ):
    monkeypatch.setattr(interrupt, '_FLUSH_SETTLE_SECONDS', 0.05)
    argv = _fake_claude(
      tmp_path,
      'stty raw -echo\n'
      "trap 'exit 0' INT\n"
      'sleep 0.2\n'
      'kill -TERM $PPID\n'
      'dd bs=1 count=1 >/dev/null 2>&1\n' + _IDLE,
    )
    with _session_terminal():
      run = _interactive(argv, tmp_path, waiters, stopped_calls)
    assert (run.code, run.stopped) == (0, True)

  def test_a_session_that_ended_itself_quits_once_its_stopped_turn_is_on_disk(
    self, tmp_path, waiters, stopped_calls, ended_session, monkeypatch
  ):
    typed: list[bytes] = []
    monkeypatch.setattr(interrupt._TerminalRun, 'type', lambda run, keys: typed.append(keys))
    transcript = tmp_path / 'transcript.jsonl'
    # an earlier stop in the same transcript, as a resumed session carries
    transcript.write_text(_stop_record('toolu_earlier') + '\n')
    stopped_calls.record(StoppedCall('toolu_ended', transcript))
    written = tmp_path / 'written'
    argv = _fake_claude(
      tmp_path,
      f"trap '[ -f {written} ] && exit 0 || exit 7' INT\n"
      'kill -TERM $PPID\n'
      'sleep 0.3\n'
      f'touch {written}\n'
      f'echo {shlex.quote(_stop_record("toolu_ended"))} >> {transcript}\n' + _IDLE,
    )
    with _session_terminal():
      run = _interactive(argv, tmp_path, waiters, stopped_calls)
    assert (run.code, run.stopped) == (0, True)
    assert typed == []
    assert waiters.stood_down()

  def test_a_session_that_ended_itself_without_its_stopped_turn_is_interrupted(
    self, tmp_path, waiters, stopped_calls, ended_session, monkeypatch
  ):
    monkeypatch.setattr(interrupt, '_STOPPED_TURN_TIMEOUT_SECONDS', 0.2)
    monkeypatch.setattr(interrupt, '_FLUSH_SETTLE_SECONDS', 0.05)
    seen_key = tmp_path / 'seen-key'
    argv = _fake_claude(
      tmp_path,
      'stty raw -echo\n'
      "trap 'exit 9' INT\n"
      'kill -TERM $PPID\n'
      "key=$(dd bs=1 count=1 2>/dev/null | od -An -tx1 | tr -d ' \\n')\n"
      f'[ "$key" = 03 ] && echo interrupted > {seen_key}\n' + _IDLE,
    )
    with _session_terminal():
      run = _interactive(argv, tmp_path, waiters, stopped_calls)
    assert (run.code, run.stopped) == (9, True)
    assert seen_key.read_text() == 'interrupted\n'

  def test_keystrokes_and_output_cross_the_proxy(self, tmp_path, waiters, stopped_calls):
    argv = _fake_claude(
      tmp_path,
      'stty raw -echo\n'
      "printf 'ready.'\n"
      'typed=$(dd bs=1 count=2 2>/dev/null)\n'
      'printf \'saw:%s.\' "$typed"\n',
    )
    echoed: list[bytes] = []
    with _session_terminal() as terminal:

      def _drive() -> None:
        _read_until(terminal, b'ready.')
        os.write(terminal, b'hi')
        echoed.append(_read_until(terminal, b'saw:hi.'))

      driver = threading.Thread(target=_drive)
      driver.start()
      run = _interactive(argv, tmp_path, waiters, stopped_calls)
      driver.join()
    assert (run.code, run.stopped) == (0, False)
    assert len(echoed) == 1

  def test_the_pty_is_sized_like_the_session_terminal(self, tmp_path, waiters, stopped_calls):
    size = tmp_path / 'size'
    argv = _fake_claude(tmp_path, f'stty size > {size}\n')
    with _session_terminal() as terminal:
      fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack('HHHH', 31, 101, 0, 0))
      run = _interactive(argv, tmp_path, waiters, stopped_calls)
    assert (run.code, run.stopped) == (0, False)
    assert size.read_text().split() == ['31', '101']


class _TranscriptClock:
  """the clock `interrupt` reads, moving only when it sleeps; a transcript
  written at each of its first `writes` sleeps, and never after."""

  def __init__(self, transcript: Path, writes: int):
    self.now = 1_000_000.0
    self.last_write = self.now
    self._transcript = transcript
    self._writes = writes
    os.utime(transcript, (self.now, self.now))

  def time(self) -> float:
    return self.now

  def monotonic(self) -> float:
    return self.now

  def sleep(self, seconds: float) -> None:
    self.now += seconds
    if self._writes > 0:
      self._writes -= 1
      with self._transcript.open('a') as stream:
        stream.write('{}\n')
      os.utime(self._transcript, (self.now, self.now))
      self.last_write = self.now


class TestFlushWait:
  def test_waits_for_the_transcript_to_stop_growing(self, tmp_path, monkeypatch):
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    transcript = transcripts / 'session.jsonl'
    transcript.write_text('{}\n')
    # written through more polls than one settle window spans
    clock = _TranscriptClock(
      transcript, writes=3 * round(interrupt._FLUSH_SETTLE_SECONDS / interrupt._POLL_SECONDS)
    )
    monkeypatch.setattr(interrupt, 'time', clock)

    interrupt._await_flush(transcripts)

    assert clock.now - clock.last_write >= interrupt._FLUSH_SETTLE_SECONDS

  def test_gives_up_on_a_transcript_that_never_settles(self, tmp_path, monkeypatch):
    monkeypatch.setattr(interrupt, '_FLUSH_GRACE_SECONDS', 0.0)
    monkeypatch.setattr(interrupt, '_FLUSH_TIMEOUT_SECONDS', 0.2)
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    (transcripts / 'session.jsonl').write_text('{}\n')
    started = time.monotonic()
    interrupt._await_flush(transcripts)
    assert time.monotonic() - started < 5.0
