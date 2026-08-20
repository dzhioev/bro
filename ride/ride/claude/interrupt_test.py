import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios
import threading
import time
from collections.abc import Generator
from pathlib import Path

import ride.claude.interrupt as interrupt

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
  while time.monotonic() < deadline:
    try:
      seen += os.read(terminal, 65536)
    except BlockingIOError:
      time.sleep(0.02)
    if needle in seen:
      return seen
  raise AssertionError(f'{needle!r} never arrived; the terminal saw {seen!r}')


class TestRunPrinting:
  def test_captures_the_printed_reply(self, tmp_path):
    run = interrupt.run_printing(_fake_claude(tmp_path, 'echo REPLY\n'), os.environ)
    assert run == interrupt.PrintedRun(code=0, output='REPLY\n', stopped=False)

  def test_a_stop_arrives_as_the_interrupt(self, tmp_path):
    argv = _fake_claude(
      tmp_path,
      "trap 'exit 7' INT\ntrap 'exit 5' TERM\nsleep 0.2\nkill -TERM $PPID\n" + _IDLE,
    )
    run = interrupt.run_printing(argv, os.environ)
    assert (run.code, run.stopped) == (7, True)

  def test_a_claude_deaf_to_the_interrupt_is_terminated(self, tmp_path, monkeypatch):
    monkeypatch.setattr(interrupt, '_EXIT_TIMEOUT_SECONDS', 0.2)
    argv = _fake_claude(tmp_path, "trap '' INT\nsleep 0.2\nkill -TERM $PPID\n" + _IDLE)
    run = interrupt.run_printing(argv, os.environ)
    assert (run.code, run.stopped) == (-signal.SIGTERM, True)


class TestRunInteractive:
  def test_a_stop_arrives_as_the_interrupt_keypress_then_quits(self, tmp_path, monkeypatch):
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
      assert interrupt.run_interactive(argv, os.environ, tmp_path / 'projects') == 9
    assert seen_key.read_text() == 'interrupted\n'

  def test_keystrokes_and_output_cross_the_proxy(self, tmp_path):
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
      assert interrupt.run_interactive(argv, os.environ, tmp_path / 'projects') == 0
      driver.join()
    assert len(echoed) == 1

  def test_the_pty_is_sized_like_the_session_terminal(self, tmp_path):
    size = tmp_path / 'size'
    argv = _fake_claude(tmp_path, f'stty size > {size}\n')
    with _session_terminal() as terminal:
      fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack('HHHH', 31, 101, 0, 0))
      assert interrupt.run_interactive(argv, os.environ, tmp_path / 'projects') == 0
    assert size.read_text().split() == ['31', '101']


class TestFlushWait:
  def test_waits_for_the_transcript_to_stop_growing(self, tmp_path, monkeypatch):
    monkeypatch.setattr(interrupt, '_FLUSH_GRACE_SECONDS', 0.0)
    monkeypatch.setattr(interrupt, '_FLUSH_SETTLE_SECONDS', 0.1)
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    transcript = transcripts / 'session.jsonl'
    transcript.write_text('{}\n')
    writing_until = time.monotonic() + 0.5

    def _write() -> None:
      while time.monotonic() < writing_until:
        with transcript.open('a') as stream:
          stream.write('{}\n')
        time.sleep(0.02)

    writer = threading.Thread(target=_write)
    writer.start()
    interrupt._await_flush(transcripts)
    assert time.monotonic() >= writing_until
    writer.join()

  def test_gives_up_on_a_transcript_that_never_settles(self, tmp_path, monkeypatch):
    monkeypatch.setattr(interrupt, '_FLUSH_GRACE_SECONDS', 0.0)
    monkeypatch.setattr(interrupt, '_FLUSH_TIMEOUT_SECONDS', 0.2)
    transcripts = tmp_path / 'projects'
    transcripts.mkdir()
    (transcripts / 'session.jsonl').write_text('{}\n')
    started = time.monotonic()
    interrupt._await_flush(transcripts)
    assert time.monotonic() - started < 5.0
