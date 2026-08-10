import asyncio
import os
import tempfile

import pytest

from bro.base.text_window import DEFAULT_LIMIT
from bro.bros.dev import jobs
from bro.bros.dev.mcp import (
  bash,
  edit_file,
  glob,
  grep,
  job,
  kill,
  read_file,
  read_reference,
  spec,
  watch,
  write_file,
)
from bro.llm.mcp import Context


def test_read_file_returns_numbered_lines():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    out = read_file(path)
    assert '    1\taaa' in out
    assert '    2\tbbb' in out
    assert '    3\tccc' in out
    # under cap → no markers
    assert 'skipped' not in out


def test_read_file_offset_emits_before_marker():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, '1\n2\n3\n4\n5\n')
    out = read_file(path, offset=2, limit=2)
    assert '    1\t' not in out
    assert '    3\t3' in out
    assert '    4\t4' in out
    assert '    5\t' not in out
    assert 'skipped before: 2 lines' in out
    assert 'skipped after: 1 lines' in out


def test_read_file_default_limit_caps_long_file():
  # produce DEFAULT_LIMIT + 50 lines; default should keep DEFAULT_LIMIT and
  # surface a `[...skipped after...]` marker noting 50 dropped lines.
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)))
    out = read_file(path)
    assert 'skipped before' not in out
    assert 'skipped after: 50 lines' in out
    assert '    1\tline 0' in out
    assert f'{DEFAULT_LIMIT:>5}\tline {DEFAULT_LIMIT - 1}' in out
    assert f'{DEFAULT_LIMIT + 1:>5}\t' not in out


def test_read_file_explicit_limit_returns_more():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, ''.join(f'line {i}\n' for i in range(DEFAULT_LIMIT + 50)))
    out = read_file(path, limit=DEFAULT_LIMIT + 50)
    assert 'skipped' not in out
    assert f'{DEFAULT_LIMIT + 50:>5}\tline {DEFAULT_LIMIT + 49}' in out


def test_write_file_overwrites_and_creates_parents():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'a', 'b', 'c.txt')
    write_file(path, 'hi')
    assert open(path).read() == 'hi'
    write_file(path, 'bye')
    assert open(path).read() == 'bye'


def test_edit_file_unique_match():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\nbbb\nccc\n')
    edit_file(path, 'bbb', 'BBB')
    assert open(path).read() == 'aaa\nBBB\nccc\n'


def test_edit_file_multi_match_without_replace_all_raises():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\naaa\n')
    with pytest.raises(ValueError, match='occurs 2 times'):
      edit_file(path, 'aaa', 'X')
    assert open(path).read() == 'aaa\naaa\n'


def test_edit_file_replace_all_replaces_every_occurrence():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\naaa\n')
    edit_file(path, 'aaa', 'X', replace_all=True)
    assert open(path).read() == 'X\nX\n'


def test_edit_file_not_found_raises():
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'f.txt')
    write_file(path, 'aaa\n')
    with pytest.raises(ValueError, match='old_string not found'):
      edit_file(path, 'zzz', 'X')


@pytest.mark.asyncio
async def test_bash_captures_stdout_and_exit_code():
  result = await bash('echo hello')
  assert 'exit_code: 0' in result
  assert 'hello' in result
  assert 'skipped' not in result


@pytest.mark.asyncio
async def test_bash_captures_stderr():
  result = await bash('echo oops 1>&2 ; false')
  assert 'exit_code: 1' in result
  assert 'oops' in result


@pytest.mark.asyncio
async def test_bash_timeout_returns_clearly():
  result = await bash('sleep 5', timeout_seconds=1)
  assert 'TIMED OUT' in result


def test_file_ops_reject_non_regular_file():
  # a FIFO would block open()/read_text forever; the file tools refuse it up front.
  with tempfile.TemporaryDirectory() as d:
    fifo = os.path.join(d, 'pipe')
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match='not a regular file'):
      read_file(fifo)
    with pytest.raises(ValueError, match='not a regular file'):
      write_file(fifo, 'x')
    with pytest.raises(ValueError, match='not a regular file'):
      edit_file(fifo, 'a', 'b')


@pytest.mark.asyncio
async def test_grep_skips_fifo_without_blocking():
  # -D skip means grep never reads a FIFO/device — even one named directly — so it
  # returns immediately instead of blocking on the open. timeout_seconds is a safety
  # net: a regression that drops -D skip fails as TIMED OUT here rather than hanging.
  with tempfile.TemporaryDirectory() as d:
    fifo = os.path.join(d, 'pipe')
    os.mkfifo(fifo)
    assert await grep('anything', path=fifo, timeout_seconds=5) == 'no matches'


@pytest.mark.asyncio
async def test_bash_long_output_emits_before_marker_keeps_tail():
  # bash tails are usually most informative — confirm we keep the LAST `limit`
  # lines and report the dropped head via a [...skipped before...] marker.
  result = await bash(f'for i in $(seq 1 {DEFAULT_LIMIT + 30}); do echo "L$i"; done')
  assert 'exit_code: 0' in result
  assert 'skipped before:' in result
  assert '30 lines' in result
  # last lines kept
  assert f'L{DEFAULT_LIMIT + 30}' in result
  # earliest lines dropped
  assert 'L1\n' not in result


@pytest.mark.asyncio
async def test_grep_finds_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'hello world\ngoodbye world\n')
    result = await grep('hello', path=d)
    assert 'hello world' in result
    assert 'skipped' not in result


@pytest.mark.asyncio
async def test_grep_no_match():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'nothing here\n')
    assert await grep('xyzzy', path=d) == 'no matches'


@pytest.mark.asyncio
async def test_grep_case_insensitive():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'HELLO\n')
    assert 'HELLO' in await grep('hello', path=d, case_insensitive=True)
    assert await grep('hello', path=d) == 'no matches'


@pytest.mark.asyncio
async def test_grep_glob_filter():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), 'target\n')
    write_file(os.path.join(d, 'a.txt'), 'target\n')
    result = await grep('target', path=d, glob='*.py')
    assert 'a.py' in result
    assert 'a.txt' not in result


@pytest.mark.asyncio
async def test_grep_explicit_limit_truncates_and_emits_after_marker():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'x\n' * 10)
    result = await grep('x', path=d, limit=3)
    assert result.count('\n') == 3  # 3 kept lines + after marker (no trailing \n)
    assert 'skipped after: 7 lines' in result


@pytest.mark.asyncio
async def test_grep_default_limit_caps_pathological_output():
  # without an explicit limit the old grep returned everything — a runaway result
  # crashed do.do. now we get DEFAULT_LIMIT lines + a marker reporting the rest.
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'f.txt'), 'match\n' * (DEFAULT_LIMIT * 3))
    result = await grep('match', path=d)
    assert 'skipped after:' in result
    assert f'{DEFAULT_LIMIT * 2:,} lines' in result


def test_glob_returns_matches_sorted_by_mtime():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'old.py'), '')
    import time

    time.sleep(0.01)
    write_file(os.path.join(d, 'new.py'), '')
    result = glob('*.py', path=d).splitlines()
    assert result[0].endswith('new.py')
    assert result[1].endswith('old.py')


def test_glob_excludes_non_matches():
  with tempfile.TemporaryDirectory() as d:
    write_file(os.path.join(d, 'a.py'), '')
    write_file(os.path.join(d, 'b.txt'), '')
    result = glob('*.py', path=d)
    assert 'a.py' in result
    assert 'b.txt' not in result


def test_glob_no_matches():
  with tempfile.TemporaryDirectory() as d:
    assert glob('*.nonexistent', path=d) == 'no matches'


def test_job_watch_kill_round_trip():
  context = Context(state=jobs.Registry())

  async def round_trip():
    started = job(context, 'echo out; echo err >&2')
    assert started.startswith('started job-1 (pid ')
    collected = await watch(context, 'job-1', wait_seconds=10, tail=True)
    assert collected.startswith('exited (code 0)\n')
    assert 'out\nerr' in collected
    long_running = job(context, 'sleep 30')
    assert long_running.startswith('started job-2')
    assert (await kill(context, 'job-2')).startswith('job-2 exited')

  asyncio.run(round_trip())


@pytest.mark.asyncio
async def test_interrupted_watch_releases_the_job_for_the_next_one():
  # the watch lock outlives the abandoned thread, so an interrupted watch has to
  # wake its job — otherwise the job stays unwatchable for the rest of a window
  # an iterative watcher may have sized in minutes.
  registry = jobs.Registry()
  context = Context(state=registry)
  job(context, 'sleep 30')
  target = registry.get('job-1')
  watching = asyncio.create_task(watch(context, 'job-1', wait_seconds=600))
  await asyncio.sleep(0.1)
  watching.cancel()
  with pytest.raises(asyncio.CancelledError):
    await watching

  # the woken thread drops the lock on its own thread, so the job frees up
  # promptly rather than by the time cancel() returns
  for _ in range(50):
    if not target._watch_lock.locked():
      break
    await asyncio.sleep(0.1)
  assert (await watch(context, 'job-1', wait_seconds=0)).startswith('running')
  registry.close()


@pytest.mark.asyncio
async def test_interrupted_bash_leaves_no_process_behind(tmp_path):
  pidfile = tmp_path / 'pid'
  running = asyncio.create_task(bash(f'echo $$ > {pidfile}; sleep 30', timeout_seconds=60))
  for _ in range(50):
    await asyncio.sleep(0.1)
    if pidfile.exists() and len(pidfile.read_text()) > 0:
      break
  pid = int(pidfile.read_text())
  running.cancel()
  with pytest.raises(asyncio.CancelledError):
    await running

  for _ in range(50):
    if not _process_alive(pid):
      break
    await asyncio.sleep(0.1)
  assert not _process_alive(pid)


def _process_alive(pid: int) -> bool:
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  # a killed child of this process lingers as a zombie until it is reaped
  return open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()[0] != 'Z'


def test_watch_unknown_job_raises():
  context = Context(state=jobs.Registry())
  with pytest.raises(ValueError, match='unknown job id'):
    asyncio.run(watch(context, 'job-1'))


def test_spec_no_args_lists_all_tools():
  server = spec().build()
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {
    'read_reference',
    'read_file',
    'write_file',
    'edit_file',
    'bash',
    'grep',
    'glob',
    'job',
    'watch',
    'kill',
  }


def test_read_reference_returns_file_contents():
  ref = read_reference()
  # canary headings — keeps the test honest if REFERENCE.md is reorganised
  assert '# dev tools reference' in ref
  assert 'Output cap (`limit`)' in ref
  assert 'Skipped-content markers' in ref
  assert 'Timeout (`timeout_seconds`)' in ref
  assert 'Fat-finger clamp' in ref
  assert 'Background jobs (`job`, `watch`, `kill`)' in ref


def test_spec_subset_filters_tools():
  server = spec('read_file', 'bash').build()
  tools = asyncio.run(server.list_tools())
  names = {t.name for t in tools}
  assert names == {'read_file', 'bash'}


def test_spec_unknown_tool_raises():
  with pytest.raises(ValueError, match='unknown dev tools'):
    spec('nope')
