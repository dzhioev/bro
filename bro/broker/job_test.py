import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from bro.base.offload import off_loop
from bro.broker.job import (
  OUTPUT_DIRECTORY,
  STATUS_FILE,
  STDERR_FILE,
  STDOUT_FILE,
  CommandJob,
  launch,
  record_status,
)

TIMEOUT = 10.0


def _python(code: str, **env: str) -> CommandJob:
  return CommandJob(command=(sys.executable, '-c', code), env=dict(env))


@pytest.mark.asyncio
async def test_clean_exit_writes_each_stream_to_its_run_file(tmp_path: Path):
  handle = await launch(
    _python('import sys; print("to-stdout"); sys.stderr.write("to-stderr\\n")'), tmp_path
  )
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  assert (tmp_path / STDOUT_FILE).read_text() == 'to-stdout\n'
  assert (tmp_path / STDERR_FILE).read_text() == 'to-stderr\n'


@pytest.mark.asyncio
async def test_the_run_directory_is_the_working_directory(tmp_path: Path):
  handle = await launch(_python('import os; print(os.getcwd())'), tmp_path)
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  assert Path((tmp_path / STDOUT_FILE).read_text().strip()) == tmp_path.resolve()


@pytest.mark.asyncio
async def test_the_run_directory_carries_an_output_directory_to_fill(tmp_path: Path):
  handle = await launch(_python(f'open("{OUTPUT_DIRECTORY}/score.json", "w").write("{{}}")'), tmp_path)  # fmt: skip
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  assert (tmp_path / OUTPUT_DIRECTORY / 'score.json').read_text() == '{}'


@pytest.mark.asyncio
async def test_failing_exit_reports_its_code(tmp_path: Path):
  handle = await launch(_python('import sys; sys.exit(3)'), tmp_path)
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 3


@pytest.mark.asyncio
async def test_env_is_the_explicit_snapshot(tmp_path: Path):
  code = 'import os; print("PATH" in os.environ, os.environ["MARKER"])'
  handle = await launch(_python(code, MARKER='7'), tmp_path)
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  assert (tmp_path / STDOUT_FILE).read_text().strip() == 'False 7'


@pytest.mark.asyncio
async def test_kill_takes_the_whole_process_group(tmp_path: Path):
  # only the background child holds the fifo's write end, so EOF on it is that
  # child gone — not merely the leader the handle reaps
  held = tmp_path / 'held'
  os.mkfifo(held)
  handle = await launch(
    CommandJob(command=('/bin/sh', '-c', f'(exec 3>{held}; sleep 3600) & sleep 3600'), env={}),
    tmp_path,
  )
  with await asyncio.wait_for(off_loop(held.open, 'rb'), TIMEOUT) as reader:
    await handle.kill()
    assert await asyncio.wait_for(handle.wait(), TIMEOUT) != 0
    assert await asyncio.wait_for(off_loop(reader.read), TIMEOUT) == b''


@pytest.mark.asyncio
async def test_kill_after_exit_is_a_no_op(tmp_path: Path):
  handle = await launch(_python('pass'), tmp_path)
  assert await asyncio.wait_for(handle.wait(), TIMEOUT) == 0
  await handle.kill()


def test_record_status_names_how_the_run_ended(tmp_path: Path):
  record_status(tmp_path, {'reason': 'timeout', 'exit_code': -15})
  assert json.loads((tmp_path / STATUS_FILE).read_text()) == {
    'reason': 'timeout',
    'exit_code': -15,
  }
