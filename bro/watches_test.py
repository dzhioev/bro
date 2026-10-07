import concurrent.futures
import contextlib
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bro import watches
from bro.base.liveness_test_helper import Liveness
from bro.base.text_window import BYTE_LIMIT, DEFAULT_LIMIT
from bro.monitor import SESSION_DIR_ENV


@pytest.fixture
def owner(monkeypatch, tmp_path):
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
  with watches.Owner.for_session() as watch_owner:
    yield watch_owner


def _seed(store: watches.Store, command: str, content: str) -> watches.Watch:
  watch = watches.Watch(command, store.directory, watches.slug(command))
  watch.command_file.write_text(f'{command}\n')
  watch.log.write_text(content)
  return watch


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


class TestStore:
  def test_take_tags_complete_lines_and_commits_offsets(self, owner):
    watch = _seed(owner.store, 'printf lines', 'one\ntwo\npartial')

    assert owner.store.take() == '[printf lines] one\n[printf lines] two'
    assert watch.saved_offset() == len('one\ntwo\n')
    assert owner.store.take() is None

    with watch.log.open('a') as log_file:
      log_file.write('\n')
    assert owner.store.take() == '[printf lines] partial'
    assert watch.saved_offset() == len('one\ntwo\npartial\n')

  def test_pending_observation_does_not_commit_and_the_last_line_is_complete(self, owner):
    watch = _seed(owner.store, 'producer', 'complete\npartial')

    assert owner.store.has_pending_lines()
    assert watch.saved_offset() == 0
    assert watch.last_complete_line() == 'complete'

    assert owner.store.take() == '[producer] complete'
    assert not owner.store.has_pending_lines()
    assert watch.last_complete_line() == 'complete'

  def test_stream_head_and_notice_memory_are_session_state(self, owner, monkeypatch):
    watch = owner.store.start('sleep 30')
    monkeypatch.setenv(watches.PRODUCER_JOURNAL_HEAD_ENV, str(watch.journal_head_file))
    monkeypatch.setenv(watches.PRODUCER_JOURNAL_WAKE_ENV, str(watch.journal_wake_file))

    with concurrent.futures.ThreadPoolExecutor() as executor:
      caught_up = executor.submit(owner.store.wait_for_journal_head, 'sleep 30', 12)
      watches.publish_journal_head(12)
      assert caught_up.result(timeout=10)

    assert watch.journal_head() == 12
    assert owner.store.mark_notified(frozenset({'mission:M1'}))
    assert not owner.store.mark_notified(frozenset({'mission:M1'}))
    assert owner.store.mark_notified(frozenset({'mission:M2'}))

  def test_take_stays_within_the_shared_bounds_and_marks_pending(self, owner):
    _seed(owner.store, 'seq', ''.join(f'{number}\n' for number in range(150)))

    first = owner.store.take()
    assert first is not None
    assert len(first.splitlines()) == DEFAULT_LIMIT
    assert len(first.encode()) <= BYTE_LIMIT
    assert first.splitlines()[-1] == '[...pending watch lines...]'

    second = owner.store.take()
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
      batches = list(executor.map(lambda _index: owner.store.take(), range(2)))

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

    first = owner.store.take()
    with a.log.open('a') as log_file:
      log_file.write(''.join(f'a{number}\n' for number in range(99, 198)))
    second = owner.store.take()

    assert first is not None and first.startswith('[a] a0\n')
    assert second is not None and second.startswith('[b] b0\n')

  def test_a_wide_line_is_paged_without_loss_and_names_its_size_once(self, owner):
    content = 'x' * (BYTE_LIMIT + 500)
    _seed(owner.store, 'wide', f'{content}\n')

    first = owner.store.take()
    second = owner.store.take()

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
    watch = watches.Watch('binary', owner.store.directory, watches.slug('binary'))
    watch.command_file.write_text('binary\n')
    watch.log.write_bytes(b'\xff' * BYTE_LIMIT + b'\n')

    batches = []
    while watch.saved_offset() < BYTE_LIMIT + 1:
      batch = owner.store.take()
      assert batch is not None
      batches.append(batch)
      assert len(batch.encode()) <= BYTE_LIMIT
      assert len(batch.splitlines()) <= DEFAULT_LIMIT

    assert len(batches) > 1
    assert watch.saved_offset() == BYTE_LIMIT + 1

  def test_a_replacement_does_not_commit_the_next_buffered_lead_byte(self, owner):
    watch = watches.Watch('binary', owner.store.directory, watches.slug('binary'))
    watch.command_file.write_text('binary\n')
    watch.log.write_bytes(b'\xc2\xc2\n')

    assert owner.store.take() == '[binary] ��'
    assert watch.saved_offset() == 3

  def test_multiline_and_oversized_commands_have_single_bounded_tags(self, owner):
    multiline = _seed(owner.store, 'first\nsecond', 'line\n')
    oversized_command = 'x' * (BYTE_LIMIT + 1)
    oversized = _seed(owner.store, oversized_command, 'line\n')

    batch = owner.store.take()

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
      batch = owner.store.take()
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
      watch.journal_head_file,
      watch.journal_wake_file,
    ):
      assert len(path.name.encode()) <= 255
    assert watch.command_file.read_text().rstrip() == command

  def test_watch_run_detaches_and_keeps_output_and_exit(self, owner):
    command = 'printf "one\\ntwo\\n"'
    watch = owner.store.start(command)

    deadline = time.monotonic() + 10
    while watch.producer_alive():
      assert time.monotonic() < deadline, 'watch producer did not exit'
      time.sleep(0.01)
    batch = owner.store.take()

    assert batch is not None
    assert f'[{command}] one' in batch
    assert f'[{command}] two' in batch
    assert f'[{command}] [watch-run] exited 0' in batch
    assert not watch.producer_alive()

  def test_stopping_a_watch_ends_its_whole_process_group(self, owner, tmp_path):
    child_path = tmp_path / 'child'
    command = f'sleep 30 & echo $! > {shlex.quote(str(child_path))}; wait'
    watch = owner.store.start(command)
    child_process_id = int(_wait_for_file(child_path))
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
      child_process_id = int(_wait_for_file(child_path))
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
      assert resumed.store.take() is None

  def test_party_members_use_their_own_session_store(self, tmp_path):
    root_directory = tmp_path / 'root' / watches.WATCH_DIRNAME
    member_directory = tmp_path / 'member' / watches.WATCH_DIRNAME
    root = watches.Store(root_directory, root_directory / '.owner')
    member = watches.Store(member_directory, member_directory / '.owner')
    root_directory.mkdir(parents=True)
    member_directory.mkdir(parents=True)
    _seed(root, 'root command', 'root\n')
    _seed(member, 'member command', 'member\n')

    assert root.take() == '[root command] root'
    assert member.take() == '[member command] member'


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
