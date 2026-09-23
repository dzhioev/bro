import io
import subprocess
import sys
import time

import pytest

from bro import watch_next, watch_run, watches
from bro.monitor import SESSION_DIR_ENV


@pytest.fixture
def session_dir(monkeypatch, tmp_path):
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
  return tmp_path


def _echo(*lines: str) -> list[str]:
  return ['bash', '-c', '; '.join(f'echo {line}' for line in lines)]


class TestWatchRun:
  def test_keeps_the_lines_and_marks_the_exit(self, session_dir, capsys):
    assert watch_run.run(_echo('one', 'two')) == 0

    (watch,) = watches.declared()
    assert watch.log.read_text() == 'one\ntwo\n[watch-run] exited 0\n'
    assert capsys.readouterr().out == 'one\ntwo\n'
    assert not watch.producer_alive()

  def test_the_exit_code_is_the_commands(self, session_dir):
    assert watch_run.run(['bash', '-c', 'exit 3']) == 3

  def test_refuses_a_second_producer_of_the_same_command(self, session_dir):
    watches.declare(_echo('one'))
    with pytest.raises(watches.WatchError, match='already runs'):
      watch_run.run(_echo('one'))

  def test_refuses_without_a_session_dir(self, monkeypatch):
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)
    with pytest.raises(watches.WatchError, match=SESSION_DIR_ENV):
      watch_run.run(_echo('one'))

  def test_the_cli_requires_a_command(self, session_dir):
    with pytest.raises(SystemExit):
      watch_run.main(['watch-run'])
    assert watch_run.main(['watch-run', *_echo('hi')]) == 0


class TestWatchNext:
  def test_delivers_new_lines_once_tagged_by_watch(self, session_dir):
    command = _echo('one', 'two')
    watch_run.run(command)
    out = io.StringIO()

    assert watch_next.wait(command, out) == 0

    tag = f'[{watches.declared(command)[0].command}]'
    assert out.getvalue() == f'{tag} one\n{tag} two\n{tag} [watch-run] exited 0\n'
    with pytest.raises(watches.WatchError, match='every watch has ended'):
      watch_next.wait(command, io.StringIO())

  def test_blocks_until_a_line_arrives(self, session_dir):
    command = ['bash', '-c', 'sleep 0.3; echo late']
    producer = subprocess.Popen(
      [sys.executable, '-m', 'bro.watch_run', *command], stdout=subprocess.DEVNULL
    )
    try:
      deadline = time.monotonic() + 5
      while len(watches.declared()) == 0:
        assert time.monotonic() < deadline, 'the producer never declared its watch'
        time.sleep(0.02)
      out = io.StringIO()
      assert watch_next.wait(command, out, poll_seconds=0.05) == 0
      assert '] late\n' in out.getvalue()
    finally:
      assert producer.wait(timeout=10) == 0

  def test_reads_every_watch_when_none_is_named(self, session_dir):
    watch_run.run(_echo('a'))
    watch_run.run(['bash', '-c', 'echo b'])
    out = io.StringIO()

    assert watch_next.wait(None, out) == 0

    assert '] a\n' in out.getvalue() and '] b\n' in out.getvalue()

  def test_a_partial_line_waits_for_its_newline(self, session_dir):
    watch = watches.declare(['tail'])
    watch.log.write_text('partial')
    assert watch.read_new() == ([], 0)
    watch.log.write_text('partial\n')
    assert watch.read_new() == (['partial'], len('partial\n'))

  def test_refuses_an_undeclared_watch_once_the_grace_passes(self, session_dir):
    with pytest.raises(watches.WatchError, match='no watch runs `nothing here`'):
      watch_next.wait(['nothing', 'here'], io.StringIO(), declaration_grace_seconds=0)
    with pytest.raises(watches.WatchError, match='no watch runs in this session'):
      watch_next.wait(None, io.StringIO(), declaration_grace_seconds=0)

  def test_waits_for_a_watch_declared_after_it_started(self, session_dir):
    command = _echo('late-declared')
    producer = subprocess.Popen(
      [
        sys.executable,
        '-c',
        'import time; time.sleep(0.4); from bro import watch_run; '
        f'raise SystemExit(watch_run.run({command!r}))',
      ],
      stdout=subprocess.DEVNULL,
    )
    try:
      out = io.StringIO()
      assert watch_next.wait(command, out, poll_seconds=0.05) == 0
      assert '] late-declared\n' in out.getvalue()
    finally:
      assert producer.wait(timeout=10) == 0


def test_slugs_keep_commands_apart_and_out_of_subdirectories():
  assert watches.slug(['quest', 'watch']) != watches.slug(['mission', 'watch'])
  assert '/' not in watches.slug(['poll-pr', 'owner/repo', '42'])
