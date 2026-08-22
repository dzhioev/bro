import asyncio
import os
import sys

import pytest

from bro.broker.job import CommandJob, launch

TIMEOUT = 10.0


def _python(code: str, **env: str) -> CommandJob:
  return CommandJob(command=(sys.executable, '-c', code), cwd=os.getcwd(), env=dict(env))


@pytest.mark.asyncio
async def test_clean_exit_with_merged_output_tail():
  handle = await launch(_python('import sys; print("to-stdout"); sys.stderr.write("to-stderr\\n")'))
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  assert 'to-stdout' in handle.output_tail()
  assert 'to-stderr' in handle.output_tail()


@pytest.mark.asyncio
async def test_failing_exit_reports_its_code():
  handle = await launch(_python('import sys; sys.exit(3)'))
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 3


@pytest.mark.asyncio
async def test_output_tail_is_ring_bounded():
  handle = await launch(_python('print("x" * 100)'), ring_bytes=8)
  await asyncio.wait_for(handle.wait(), TIMEOUT)
  assert len(handle.output_tail()) == 8


@pytest.mark.asyncio
async def test_env_is_the_explicit_snapshot():
  code = 'import os; print("PATH" in os.environ, os.environ["MARKER"])'
  handle = await launch(_python(code, MARKER='7'))
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  assert 'False 7' in handle.output_tail()  # nothing of this process's environment leaked


@pytest.mark.asyncio
async def test_kill_takes_the_whole_process_group():
  # the shell's background child inherits the group and holds the output pipe;
  # only a group-wide kill lets wait()'s drain reach EOF instead of hanging on
  # the surviving child's copy of the write end.
  handle = await launch(
    CommandJob(command=('/bin/sh', '-c', 'sleep 3600 & sleep 3600'), cwd=os.getcwd(), env={})
  )
  await handle.kill()
  code = await asyncio.wait_for(handle.wait(), TIMEOUT)
  assert code != 0


@pytest.mark.asyncio
async def test_kill_after_exit_is_a_no_op():
  handle = await launch(_python('pass'))
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  await handle.kill()
