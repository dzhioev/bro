import concurrent.futures
import contextlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bro import watch_run, watches
from bro.base.liveness_test_helper import Liveness
from bro.base.text_window import BYTE_LIMIT, DEFAULT_LIMIT
from bro.brash import REFUSED_STATUS, Policy
from bro.monitor import SESSION_DIR_ENV


@pytest.fixture
def owner(monkeypatch, tmp_path):
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
  with watches.Owner.for_session() as watch_owner:
    yield watch_owner


def _seed(store: watches.Store, command: str, content: str | bytes) -> watches.Watch:
  watch = watches.Watch(command, store.directory, watches.slug(command))
  watch.command_file.write_text(command)
  _append(watch, content)
  return watch


def _append(watch: watches.Watch, content: str | bytes) -> None:
  """append `content` to the watch's log as its producer does."""
  with watches.LineLog.open(watch) as line_log:
    line_log.write(content.encode() if isinstance(content, str) else content)


def _taken(store: watches.Store) -> str | None:
  """the next batch's text as the model reads it."""
  batch = store.take()
  return None if batch is None else batch.text()


def _process_is_running(process_id: int) -> bool:
  try:
    state = Path(f'/proc/{process_id}/stat').read_text().split()[2]
  except (FileNotFoundError, ProcessLookupError):
    return False
  return state != 'Z'


def _wait_for_file(path: Path) -> str:
  deadline = time.monotonic() + 10
  while not path.exists():
    assert time.monotonic() < deadline, f'{path} was not created'
    time.sleep(0.01)
  return path.read_text()


def _wait_for_nonempty_file(path: Path) -> str:
  deadline = time.monotonic() + 10
  while True:
    try:
      content = path.read_text()
    except FileNotFoundError:
      content = ''
    if content:
      return content
    assert time.monotonic() < deadline, f'{path} was not populated'
    time.sleep(0.01)


class _Clock:
  """the time module, its epoch clock reading `instants` in turn, in seconds."""

  def __init__(self, *instants: int) -> None:
    self._instants = iter(instants)

  def time_ns(self) -> int:
    return next(self._instants) * 1_000_000_000

  def __getattr__(self, name: str) -> object:
    return getattr(time, name)


class TestArrivals:
  def test_a_line_arrives_when_its_first_byte_does(self, owner, monkeypatch):
    monkeypatch.setattr(watches, 'time', _Clock(1, 2))
    watch = _seed(owner.store, 'producer', 'par')
    _append(watch, f'tial\n{watches.quiet("next")}\n')

    batch = owner.store.take()

    assert batch is not None
    assert batch.lines == (
      watches.BatchLine('producer', 'partial', wakes=True, arrived=1.0),
      watches.BatchLine('producer', 'next', wakes=False, arrived=2.0),
    )

  def test_a_line_split_across_batches_keeps_its_arrival(self, owner, monkeypatch):
    monkeypatch.setattr(watches, 'time', _Clock(5))
    _seed(owner.store, 'wide', f'{"x" * (BYTE_LIMIT * 2)}\n')

    first = owner.store.take()
    second = owner.store.take()

    assert first is not None and second is not None
    assert {line.arrived for line in first.lines + second.lines} == {5.0}

  def test_a_quiet_line_wakes_the_session_only_where_its_watch_wakes_on_quiet(self, owner):
    _seed(owner.store, 'quiet', f'{watches.quiet("held")}\n')
    _seed(owner.store, 'loud', f'{watches.quiet("woke")}\n')
    owner.store.set_wake_on_quiet('loud', True)

    batch = owner.store.take()

    assert batch is not None
    assert {line.content: line.wakes for line in batch.lines} == {'held': False, 'woke': True}

  def test_a_line_without_its_arrival_fails_the_take(self, owner):
    watch = watches.Watch('raw', owner.store.directory, watches.slug('raw'))
    watch.command_file.write_text('raw')
    watch.log.write_text('written past the producer\n')

    with pytest.raises(watches.WatchError, match='no arrival time'):
      owner.store.take()


class TestCopier:
  def test_a_head_is_published_once_the_output_sent_before_it_is_in_the_log(
    self, tmp_path, monkeypatch
  ):
    published: dict[int, bytes] = {}

    def publish(watch: watches.Watch, head: int) -> None:
      published[head] = watch.log.read_bytes()

    monkeypatch.setattr(watches.Watch, 'publish_journal_head', publish)
    # in-process, a failed copy would end the test runner's own process group
    monkeypatch.setattr(watch_run.job_supervisor, 'end_group', lambda: None)
    watch = watches.Watch('producer', tmp_path, watches.slug('producer'))
    output_read, output_write = os.pipe()
    journal_read, journal_write = os.pipe()
    # both are queued before the copy starts, the head after the output
    os.write(output_write, b'before\n')
    os.write(journal_write, b'7\n')

    with (
      watches.LineLog.open(watch) as line_log,
      os.fdopen(output_read, 'rb', 0) as output,
      os.fdopen(journal_read, 'rb', 0) as journal,
      os.fdopen(journal_write, 'wb', 0),
    ):
      copier = watch_run._Copier(watch, line_log, output.fileno(), journal.fileno())
      os.close(output_write)
      copier.finish()

    assert published == {7: b'before\n'}

  def test_a_head_is_published_while_later_output_keeps_coming(self, tmp_path, monkeypatch):
    # the writes after which the output stops coming
    refill_limit = 100
    refills = 0
    published: dict[int, int] = {}

    def publish(watch: watches.Watch, head: int) -> None:
      published[head] = refills

    class Refilling(watches.LineLog):
      """a log each write to which is followed by more output"""

      def write(self, data: bytes) -> None:
        nonlocal refills
        super().write(data)
        if refills < refill_limit:
          refills += 1
          os.write(output_write, b'later\n')

    monkeypatch.setattr(watches.Watch, 'publish_journal_head', publish)
    # in-process, a failed copy would end the test runner's own process group
    monkeypatch.setattr(watch_run.job_supervisor, 'end_group', lambda: None)
    watch = watches.Watch('producer', tmp_path, watches.slug('producer'))
    output_read, output_write = os.pipe()
    journal_read, journal_write = os.pipe()
    os.write(output_write, b'before\n')
    os.write(journal_write, b'7\n')

    with (
      Refilling.open(watch) as line_log,
      os.fdopen(output_read, 'rb', 0) as output,
      os.fdopen(journal_read, 'rb', 0) as journal,
      os.fdopen(journal_write, 'wb', 0),
    ):
      copier = watch_run._Copier(watch, line_log, output.fileno(), journal.fileno())
      deadline = time.monotonic() + 10
      while refills < refill_limit:
        assert time.monotonic() < deadline, 'the copy stopped copying the output'
        time.sleep(0.01)
      os.close(output_write)
      copier.finish()

    assert published[7] < refill_limit


class TestStore:
  def test_take_tags_complete_lines_and_commits_offsets(self, owner):
    watch = _seed(owner.store, 'printf lines', 'one\ntwo\npartial')

    assert _taken(owner.store) == '[printf lines] one\n[printf lines] two'
    assert watch.saved_offset() == len('one\ntwo\n')
    assert _taken(owner.store) is None

    _append(watch, '\n')
    assert _taken(owner.store) == '[printf lines] partial'
    assert watch.saved_offset() == len('one\ntwo\npartial\n')

  def test_pending_observation_does_not_commit_and_the_last_line_is_complete(self, owner):
    watch = _seed(owner.store, 'producer', 'complete\npartial')

    assert owner.store.has_waking_lines()
    assert watch.saved_offset() == 0
    assert watch.last_complete_line() == 'complete'

    assert _taken(owner.store) == '[producer] complete'
    assert not owner.store.has_waking_lines()
    assert watch.last_complete_line() == 'complete'

  def test_quiet_lines_wait_for_a_waking_line_and_arrive_unmarked_in_order(self, owner):
    watch = _seed(owner.store, 'producer', f'{watches.quiet("first")}\n{watches.quiet("second")}\n')

    assert not owner.store.has_waking_lines()
    assert _taken(owner.store) is None
    assert watch.saved_offset() == 0
    assert watch.last_complete_line() == 'second'

    _append(watch, 'third\n')
    assert owner.store.has_waking_lines()
    assert _taken(owner.store) == '[producer] first\n[producer] second\n[producer] third'
    assert not owner.store.has_waking_lines()

  def test_a_waking_line_carries_the_quiet_lines_of_every_watch(self, owner):
    _seed(owner.store, 'a', f'{watches.quiet("quiet")}\n')
    _seed(owner.store, 'b', 'wake\n')

    batch = _taken(owner.store)

    assert batch is not None
    assert sorted(batch.splitlines()) == ['[a] quiet', '[b] wake']

  def test_a_quiet_line_cut_across_batches_stays_quiet(self, owner):
    content = 'q' * (BYTE_LIMIT + 500)
    size_marker = f'[line: {(len(content) / 1_000):.1f} KB] '
    watch = _seed(owner.store, 'wide', f'wake\n{watches.quiet(content)}\n')

    first = _taken(owner.store)
    assert first is not None and first.startswith('[wide] wake\n')
    assert '[...pending watch lines...]' not in first
    assert _taken(owner.store) is None

    _append(watch, 'later\n')
    second = _taken(owner.store)
    assert second is not None and second.endswith('\n[wide] later')
    delivered = ''.join(
      line.removeprefix('[wide] ').removeprefix(size_marker)
      for batch in (first, second)
      for line in batch.splitlines()
      if line not in ('[wide] wake', '[wide] later')
    )
    assert delivered == content

  def test_a_watch_set_to_wake_on_quiet_lines_wakes_on_them(self, owner):
    _seed(owner.store, 'producer', f'{watches.quiet("quiet")}\n')

    owner.store.set_wake_on_quiet('producer', True)
    assert owner.store.has_waking_lines()
    owner.store.set_wake_on_quiet('producer', False)
    assert _taken(owner.store) is None
    owner.store.set_wake_on_quiet('producer', True)
    assert _taken(owner.store) == '[producer] quiet'

    with pytest.raises(watches.WatchError, match='no watch runs'):
      owner.store.set_wake_on_quiet('absent', True)

  def test_a_published_journal_head_certifies_the_output_written_before_it(self, owner):
    # output wider than the pipe holds, so the copy is still behind when the
    # command publishes its head
    script = (
      'import sys, time; from bro import watches; '
      f'sys.stdout.write("x" * {BYTE_LIMIT * 4} + "\\n"); sys.stdout.write("last\\n"); '
      'sys.stdout.flush(); watches.publish_journal_head(12); time.sleep(30)'
    )
    command = shlex.join([sys.executable, '-c', script])
    watch = owner.store.start(command)

    assert owner.store.wait_for_journal_head(command, 12)

    assert watch.journal_head() == 12
    assert watch.last_complete_line() == 'last'

  def test_notice_memory_is_session_state(self, owner):
    assert owner.store.mark_notified(frozenset({'mission:M1'}))
    assert not owner.store.mark_notified(frozenset({'mission:M1'}))
    assert owner.store.mark_notified(frozenset({'mission:M2'}))

  def test_take_stays_within_the_shared_bounds_and_marks_pending(self, owner):
    _seed(owner.store, 'seq', ''.join(f'{number}\n' for number in range(150)))

    first = _taken(owner.store)
    assert first is not None
    assert len(first.splitlines()) == DEFAULT_LIMIT
    assert len(first.encode()) <= BYTE_LIMIT
    assert first.splitlines()[-1] == '[...pending watch lines...]'

    second = _taken(owner.store)
    assert second is not None
    delivered = [
      int(line.removeprefix('[seq] '))
      for batch in (first, second)
      for line in batch.splitlines()
      if line.startswith('[seq] ')
    ]
    assert delivered == list(range(150))

  def test_two_readers_commit_disjoint_batches_under_the_lock(self, owner):
    _seed(owner.store, 'seq', ''.join(f'{number}\n' for number in range(180)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
      batches = list(executor.map(lambda _index: _taken(owner.store), range(2)))

    delivered = [
      int(line.removeprefix('[seq] '))
      for batch in batches
      if batch is not None
      for line in batch.splitlines()
      if line.startswith('[seq] ')
    ]
    assert sorted(delivered) == list(range(180))
    assert len(delivered) == len(set(delivered))

  def test_a_cut_watch_yields_the_first_turn_of_the_next_batch(self, owner):
    a = _seed(owner.store, 'a', ''.join(f'a{number}\n' for number in range(99)))
    _seed(owner.store, 'b', 'b0\n')

    first = _taken(owner.store)
    _append(a, ''.join(f'a{number}\n' for number in range(99, 198)))
    second = _taken(owner.store)

    assert first is not None and first.startswith('[a] a0\n')
    assert second is not None and second.startswith('[b] b0\n')

  def test_a_wide_line_is_paged_without_loss_and_names_its_size_once(self, owner):
    content = 'x' * (BYTE_LIMIT + 500)
    _seed(owner.store, 'wide', f'{content}\n')

    first = _taken(owner.store)
    second = _taken(owner.store)

    assert first is not None and second is not None
    assert f'[line: {(len(content) / 1_000):.1f} KB]' in first
    assert '[line:' not in second
    delivered = ''.join(
      line.split('] ', 1)[1].removeprefix(f'[line: {(len(content) / 1_000):.1f} KB] ')
      for batch in (first, second)
      for line in batch.splitlines()
      if line.startswith('[wide] ')
    )
    assert delivered == content

  def test_invalid_utf8_expansion_stays_bounded_and_advances(self, owner):
    watch = _seed(owner.store, 'binary', b'\xff' * BYTE_LIMIT + b'\n')

    batches = []
    while watch.saved_offset() < BYTE_LIMIT + 1:
      batch = _taken(owner.store)
      assert batch is not None
      batches.append(batch)
      assert len(batch.encode()) <= BYTE_LIMIT
      assert len(batch.splitlines()) <= DEFAULT_LIMIT

    assert len(batches) > 1
    assert watch.saved_offset() == BYTE_LIMIT + 1

  def test_a_replacement_does_not_commit_the_next_buffered_lead_byte(self, owner):
    watch = _seed(owner.store, 'binary', b'\xc2\xc2\n')

    assert _taken(owner.store) == '[binary] ��'
    assert watch.saved_offset() == 3

  def test_multiline_and_oversized_commands_have_single_bounded_tags(self, owner):
    multiline = _seed(owner.store, 'first\nsecond', 'line\n')
    oversized_command = 'x' * (BYTE_LIMIT + 1)
    oversized = _seed(owner.store, oversized_command, 'line\n')

    batch = _taken(owner.store)

    assert batch is not None
    lines = batch.splitlines()
    assert lines[0] == r'[first\nsecond] line'
    assert len(batch.encode()) <= BYTE_LIMIT
    assert len(lines) == 2
    assert 'sha256:' in lines[1]
    assert multiline.saved_offset() == len('line\n')
    assert oversized.saved_offset() == len('line\n')

  def test_a_large_line_pages_by_advancing_its_cursor(self, owner):
    content = 'z' * (BYTE_LIMIT * 4)
    watch = _seed(owner.store, 'large', f'{content}\n')
    offsets = []

    while watch.saved_offset() < len(content) + 1:
      batch = _taken(owner.store)
      assert batch is not None
      offsets.append(watch.saved_offset())

    assert offsets == sorted(set(offsets))
    assert offsets[-1] == len(content) + 1


class TestProducers:
  def test_producer_start_observes_an_owner_that_already_closed(self, tmp_path):
    directory = tmp_path / 'closed-owner'
    owner_path = directory / '.owner'
    script = (
      'import os; from pathlib import Path; from bro import watches; '
      f'directory = Path({str(directory)!r}); directory.mkdir(); '
      f'owner_path = Path({str(owner_path)!r}); os.mkfifo(owner_path); '
      'store = watches.Store(directory, owner_path); '
      'watch = store.start("sleep 30"); identity = watch.producer_identity(); '
      'print(-1 if identity is None else identity.process_id)'
    )

    result = subprocess.run(
      [sys.executable, '-c', script],
      check=True,
      capture_output=True,
      text=True,
      timeout=10,
    )
    producer_process_id = int(result.stdout)
    deadline = time.monotonic() + 10
    while producer_process_id >= 0 and _process_is_running(producer_process_id):
      assert time.monotonic() < deadline, 'producer survived its absent owner'
      time.sleep(0.01)

  def test_multibyte_command_names_stay_within_filesystem_component_bounds(self, owner):
    command = f'printf ok # {"界" * 80}'
    watch = owner.store.start(command)

    for path in (
      watch.command_file,
      watch.log,
      watch.offset_file,
      watch.pid_file,
      watch.journal_file,
      watch.journal_head_file,
      watch.journal_wake_file,
      watch.wake_on_quiet_file,
    ):
      assert len(path.name.encode()) <= 255
    assert watch.command_file.read_text() == command

  def test_watch_run_detaches_and_keeps_output_and_exit(self, owner):
    command = 'printf "one\\ntwo\\n"'
    watch = owner.store.start(command)

    deadline = time.monotonic() + 10
    while watch.producer_alive():
      assert time.monotonic() < deadline, 'watch producer did not exit'
      time.sleep(0.01)
    batch = _taken(owner.store)

    assert batch is not None
    assert f'[{command}] one' in batch
    assert f'[{command}] two' in batch
    assert f'[{command}] [watch-run] exited 0' in batch
    assert not watch.producer_alive()

  def test_an_exit_after_an_unterminated_quiet_line_still_wakes(self, owner):
    command = 'printf "\\037quiet"; exit 7'
    watch = owner.store.start(command)

    deadline = time.monotonic() + 10
    while watch.producer_alive():
      assert time.monotonic() < deadline, 'watch producer did not exit'
      time.sleep(0.01)

    assert _taken(owner.store) == f'[{command}] quiet\n[{command}] [watch-run] exited 7'

  def test_a_producer_started_to_wake_on_quiet_lines_wakes_on_them(self, owner):
    command = 'printf "\\037quiet\\n"; sleep 30'
    watch = owner.store.start(command, wake_on_quiet=True)

    deadline = time.monotonic() + 10
    while (batch := _taken(owner.store)) is None:
      assert time.monotonic() < deadline, 'the quiet line never woke the store'
      time.sleep(0.01)
    owner.store.stop(command)

    assert batch == f'[{command}] quiet'
    assert not watch.wake_on_quiet_file.exists()

  def test_a_watch_under_a_brash_policy_runs_its_line_in_brash(self, owner, tmp_path):
    policy = tmp_path / 'brash-policy.json'
    Policy(entries=('printf ...',), writable=False).write(policy)
    command = 'printf "listed\\n"; cat /dev/null'
    watch = owner.store.start(command, policy)

    deadline = time.monotonic() + 10
    while watch.producer_alive():
      assert time.monotonic() < deadline, 'watch producer did not exit'
      time.sleep(0.01)
    batch = _taken(owner.store)

    assert batch is not None
    refusal, exit_line = batch.splitlines()
    assert refusal.startswith(f"[{command}] brash: refused 'cat'")
    assert exit_line == f'[{command}] [watch-run] exited {REFUSED_STATUS}'

  def test_a_watch_runs_and_keeps_its_line_untouched(self, owner, tmp_path):
    policy = tmp_path / 'brash-policy.json'
    Policy(entries=('printf ...',), writable=False).write(policy)
    command = "printf '<%s>\\n' x\\ "
    watch = owner.store.start(command, policy)

    deadline = time.monotonic() + 10
    while watch.producer_alive():
      assert time.monotonic() < deadline, 'watch producer did not exit'
      time.sleep(0.01)

    assert [declared.command for declared in owner.store.declared()] == [command]
    assert _taken(owner.store) == f'[{command}] <x >\n[{command}] [watch-run] exited 0'

  def test_a_watch_runs_the_runtimes_producer_whatever_its_directory_holds(
    self, owner, tmp_path, monkeypatch
  ):
    # a session works in an operated checkout that may carry another version of bro
    checkout = tmp_path / 'checkout'
    (checkout / 'bro').mkdir(parents=True)
    (checkout / 'bro' / 'watch_run.py').write_text('raise SystemExit("the checkout ran")\n')
    monkeypatch.chdir(checkout)

    watch = owner.store.start('echo produced')
    deadline = time.monotonic() + 10
    while watch.producer_alive():
      assert time.monotonic() < deadline, 'watch producer did not exit'
      time.sleep(0.01)

    assert _taken(owner.store) == '[echo produced] produced\n[echo produced] [watch-run] exited 0'

  def test_a_failed_copy_ends_the_producers_group(self, owner, monkeypatch, tmp_path):
    # a producer whose log can grow no further than this many bytes
    limit = 65_536
    launch = (
      f'import resource, runpy; resource.setrlimit(resource.RLIMIT_FSIZE, ({limit}, {limit})); '
      'runpy.run_module("bro.watch_run", run_name="__main__", alter_sys=True)'
    )
    monkeypatch.setattr(watches.spawn, 'module_argv', lambda module: [sys.executable, '-c', launch])
    command_pid = tmp_path / 'command.pid'
    watch = owner.store.start(f'echo $$ > {shlex.quote(str(command_pid))}; exec yes')

    deadline = time.monotonic() + 30
    while watch.producer_alive() or not command_pid.exists():
      assert time.monotonic() < deadline, 'a producer whose copy failed kept running'
      time.sleep(0.05)
    while _process_is_running(int(command_pid.read_text())):
      assert time.monotonic() < deadline, 'the command outlived its failed copy'
      time.sleep(0.05)

    assert not watch.pid_file.exists()

  def test_stopping_a_watch_ends_its_whole_process_group(self, owner, tmp_path):
    child_path = tmp_path / 'child'
    command = f'sleep 30 & echo $! > {shlex.quote(str(child_path))}; wait'
    watch = owner.store.start(command)
    child_process_id = int(_wait_for_nonempty_file(child_path))
    identity = watch.producer_identity()
    assert identity is not None

    owner.store.stop(command)

    assert not _process_is_running(identity.process_id)
    assert not _process_is_running(child_process_id)

  def test_owner_exit_stops_producers_while_the_embedding_process_lives(
    self, monkeypatch, tmp_path
  ):
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path / 'session'))
    child_path = tmp_path / 'owned-child'
    command = f'sleep 30 & echo $! > {shlex.quote(str(child_path))}; wait'
    with watches.Owner.for_session() as watch_owner:
      watch = watch_owner.store.start(command)
      child_process_id = int(_wait_for_nonempty_file(child_path))
      identity = watch.producer_identity()
      assert identity is not None

    assert not _process_is_running(identity.process_id)
    assert not _process_is_running(child_process_id)

  def test_owner_process_death_escalates_past_a_term_resistant_watch(self, tmp_path):
    with contextlib.closing(Liveness(tmp_path / 'abrupt-liveness')) as liveness:
      ready_path = tmp_path / 'term-resistant-ready'
      command = liveness.holding(
        f"trap '' TERM; touch {shlex.quote(str(ready_path))}; exec sleep 30"
      )
      session = tmp_path / 'abrupt-session'
      script = (
        'import os; '
        f'os.environ[{SESSION_DIR_ENV!r}] = {str(session)!r}; '
        'from bro import watches; '
        'owner = watches.Owner.for_session(); owner.__enter__(); '
        f'owner.store.start({command!r}); '
        'print("started", flush=True); os.read(0, 1); os._exit(0)'
      )
      process = subprocess.Popen(
        [sys.executable, '-c', script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
      )
      assert process.stdin is not None
      assert process.stdout is not None
      assert process.stdout.readline() == 'started\n'
      _wait_for_file(ready_path)
      with process.stdin:
        process.stdin.write('x')
        process.stdin.flush()
      assert process.wait(timeout=10) == 0
      liveness.assert_reaped()

  def test_unwatch_refuses_the_runtime_owned_session_watch(self, owner):
    with pytest.raises(watches.WatchError, match='owned by the runtime'):
      owner.store.stop(watches.SESSION_WATCH_COMMAND)


class TestOwner:
  def test_a_new_owner_starts_with_an_empty_store(self, monkeypatch, tmp_path):
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    with watches.Owner.for_session() as first:
      watch = _seed(first.store, 'old', 'uncommitted\n[watch-run] exited 0\n')
      watch.pid_file.unlink(missing_ok=True)

    with watches.Owner.for_session() as resumed:
      assert resumed.store.declared() == []
      assert _taken(resumed.store) is None

  def test_party_members_use_their_own_session_store(self, tmp_path):
    root_directory = tmp_path / 'root' / watches.WATCH_DIRNAME
    member_directory = tmp_path / 'member' / watches.WATCH_DIRNAME
    root = watches.Store(root_directory, root_directory / '.owner')
    member = watches.Store(member_directory, member_directory / '.owner')
    root_directory.mkdir(parents=True)
    member_directory.mkdir(parents=True)
    _seed(root, 'root command', 'root\n')
    _seed(member, 'member command', 'member\n')

    assert _taken(root) == '[root command] root'
    assert _taken(member) == '[member command] member'


@pytest.mark.parametrize(
  'may_summon,summoned,talk,expected',
  [
    (('reviewer',), False, None, True),
    ((), True, ('owner.say',), True),
    ((), True, ('owner.question',), True),
    ((), True, ('worker.question',), True),
    ((), True, ('worker.say',), False),
    ((), False, ('owner.say',), False),
  ],
)
def test_session_watch_admission(may_summon, summoned, talk, expected):
  assert (
    watches.session_watch_admitted(may_summon=may_summon, summoned=summoned, talk=talk) is expected
  )


def test_slugs_keep_commands_apart_and_out_of_subdirectories():
  assert watches.slug(['quest', 'watch']) != watches.slug(['mission', 'watch'])
  assert '/' not in watches.slug(['poll-pr', 'owner/repo', '42'])
