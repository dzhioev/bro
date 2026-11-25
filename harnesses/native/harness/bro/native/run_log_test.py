import datetime
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from bro.llm.tracker import RunFork
from bro.native import run_log
from bro.native.run_log import Reader, RunLog, RunLogError
from bro.trails.model import tools_sha256
from bro.workspace.paths import runtime_base


def started(path: Path, **options) -> RunLog:
  writer = RunLog(path)
  writer.start('dev', {'type': 'echo', 'model': 'echo'}, 'system', None, False, 'test', **options)
  return writer


def entries(path: Path) -> list[dict]:
  return [json.loads(line) for line in path.read_text().splitlines()]


def test_steps_tools_times_and_verbatim_lines(tmp_path):
  path = tmp_path / 'private' / 'nested' / 'run.jsonl'
  writer = started(path)
  assert writer.step('user_input', 'héllo\nworld', turn_index=0) == 1
  tools = [{'name': 'example', 'inputSchema': {'type': 'object'}}]
  body = {'request': {'tools': tools, 'model': 'echo'}, 'response': {}}
  for index in range(2):
    assert writer.step('llm_call', body, wire='echo', turn_index=0, call_index=index) == index + 2
  assert body['request']['tools'] == tools
  writer.end('ok')
  values = entries(path)
  assert [value['kind'] for value in values] == [
    'open',
    'system_prompt',
    'user_input',
    'tools',
    'llm_call',
    'llm_call',
    'end',
  ]
  assert [value['tools_sha256'] for value in values if value['kind'] == 'llm_call'] == [
    tools_sha256(tools)
  ] * 2
  assert all(
    'tools' not in value['body']['request'] for value in values if value['kind'] == 'llm_call'
  )
  for value in values:
    datetime.datetime.strptime(value['at'], '%Y-%m-%dT%H:%M:%S.%fZ')
  reader = Reader(path)
  result = reader.read(max_entries=100, max_bytes=100000)
  assert [entry.value for entry in result] == values
  assert [entry.line for entry in result] == path.read_text().splitlines()
  assert [entry.step_number for entry in result] == [None, 0, 1, None, 2, 3, None]
  assert reader.offset == path.stat().st_size
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
  assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


def test_default_location_and_fork(tmp_path):
  writer = RunLog()
  writer.start('dev', {}, 'system', RunFork('parent', 7), True, 'call')
  assert writer.path.parent == runtime_base() / 'bro' / 'runs'
  assert writer.path.suffix == '.jsonl'
  value = entries(writer.path)[0]
  assert value['forked_from'] == {'run': 'parent', 'step': 7}
  assert value['interactive'] is True
  assert value['recipe'] == {}


def test_creation_is_exclusive(tmp_path):
  path = tmp_path / 'run.jsonl'
  started(path)
  previous = path.read_bytes()
  with pytest.raises(FileExistsError):
    started(path)
  assert path.read_bytes() == previous


def test_failed_write_poisoning_and_best_effort_end(tmp_path, monkeypatch):
  path = tmp_path / 'run.jsonl'
  writer = started(path)
  with monkeypatch.context() as patch:

    def fail(*args, **kwargs):
      raise OSError('disk full')

    patch.setattr(run_log.os, 'open', fail)
    with pytest.raises(OSError, match='disk full'):
      writer.step('user_input', 'lost')
  with pytest.raises(RuntimeError, match='failed write'):
    writer.step('user_input', 'later')
  writer.end('error', 'OSError: disk full')
  assert [entry['kind'] for entry in entries(path)] == ['open', 'system_prompt']


def test_end_is_once_and_refuses_later_steps(tmp_path):
  path = tmp_path / 'run.jsonl'
  writer = started(path)
  writer.end('raised', 'reason')
  writer.end('ok')
  with pytest.raises(RuntimeError, match='after end'):
    writer.step('user_input', 'late')
  assert entries(path)[-1]['detail'] == 'reason'
  assert sum(value['kind'] == 'end' for value in entries(path)) == 1


def test_threaded_steps_are_serialized(tmp_path):
  path = tmp_path / 'run.jsonl'
  writer = started(path)
  with ThreadPoolExecutor() as executor:
    numbers = list(executor.map(lambda value: writer.step('user_input', str(value)), range(100)))
  assert sorted(numbers) == list(range(1, 101))
  result = Reader(path).read(max_entries=1000, max_bytes=100000)
  assert [entry.step_number for entry in result[1:]] == list(range(101))


def test_incomplete_tail_waits_for_newline(tmp_path):
  path = tmp_path / 'run.jsonl'
  started(path)
  reader = Reader(path)
  reader.read(max_entries=10, max_bytes=100000)
  offset = reader.offset
  tail = json.dumps({'kind': 'user_input', 'at': '2026-01-01T00:00:00.000000Z', 'body': 'next'})
  with path.open('a') as stream:
    stream.write(tail)
  assert reader.read(max_entries=10, max_bytes=100000) == []
  assert reader.offset == offset
  with path.open('a') as stream:
    stream.write('\n')
  assert reader.read(max_entries=10, max_bytes=100000)[0].line == tail


def test_read_bounds_and_oversize_progress(tmp_path):
  path = tmp_path / 'run.jsonl'
  writer = started(path)
  writer.step('user_input', 'x' * 1000)
  reader = Reader(path)
  first = reader.read(max_entries=1, max_bytes=1)
  assert len(first) == 1
  second = reader.read(
    max_entries=10, max_bytes=len(path.read_bytes().splitlines(keepends=True)[1])
  )
  assert len(second) == 1
  assert second[0].step_number == 0
  assert len(reader.read(max_entries=10, max_bytes=1)) == 1
  assert reader.read(max_entries=10, max_bytes=1) == []
  with pytest.raises(ValueError, match='positive'):
    reader.read(max_entries=0, max_bytes=1)


@pytest.mark.parametrize(
  'violation',
  [
    'missing_open',
    'duplicate_open',
    'unknown_format',
    'wrong_step_zero',
    'wrong_turn_zero',
    'bad_digest',
    'duplicate_tools',
    'unknown_tools',
    'duplicate_end',
    'after_end',
    'missing_at',
    'unknown_kind',
    'non_object',
    'invalid_json',
  ],
)
def test_reader_refuses_invalid_logs_naming_file_and_line(tmp_path, violation):
  path = tmp_path / 'run.jsonl'
  writer = started(path)
  writer.end('ok')
  values: list[Any] = entries(path)
  tool = {'kind': 'tools', 'at': values[0]['at'], 'sha256': tools_sha256([]), 'tools': []}
  if violation == 'missing_open':
    values.pop(0)
  elif violation == 'duplicate_open':
    values.insert(1, values[0])
  elif violation == 'unknown_format':
    values[0]['format'] = 1
  elif violation == 'wrong_step_zero':
    values[1]['kind'] = 'user_input'
  elif violation == 'wrong_turn_zero':
    values[1]['turn_index'] = 1
  elif violation == 'bad_digest':
    values.insert(2, {**tool, 'sha256': 'wrong'})
  elif violation == 'duplicate_tools':
    values[2:2] = [tool, tool]
  elif violation == 'unknown_tools':
    values.insert(
      2, {'kind': 'llm_call', 'at': values[0]['at'], 'body': {}, 'tools_sha256': tools_sha256([])}
    )
  elif violation == 'duplicate_end':
    values.append(values[-1])
  elif violation == 'after_end':
    values.append(values[1])
  elif violation == 'missing_at':
    del values[1]['at']
  elif violation == 'unknown_kind':
    values[1]['kind'] = 'other'
  elif violation == 'non_object':
    values[1] = []
  path.write_text('\n'.join(json.dumps(value) for value in values) + '\n')
  if violation == 'invalid_json':
    path.write_text('{\n')
  with pytest.raises(RunLogError, match=str(path) + r':\d+:'):
    Reader(path).read(max_entries=100, max_bytes=100000)


@pytest.mark.parametrize('offset', [-1, 7, True, None])
def test_reader_refuses_a_missing_or_out_of_bounds_session_offset(tmp_path, offset):
  path = tmp_path / 'run.jsonl'
  started(path)
  values = entries(path)
  values[1]['session_at'] = offset
  path.write_text(''.join(json.dumps(value) + '\n' for value in values))
  with pytest.raises(RunLogError, match=f'{path}.*session_at'):
    run_log.read(path)


@pytest.mark.parametrize('missing', ['file', 'step', 'cycle'])
def test_chain_refuses_missing_origins_and_cycles(tmp_path, missing):
  parent = started(tmp_path / 'parent.jsonl')
  parent.step('user_input', 'hello')
  child = RunLog(tmp_path / 'child.jsonl')
  child.start('dev', {}, 'child', RunFork('parent', 1), True, 'test')
  if missing == 'file':
    parent.path.unlink()
  elif missing == 'step':
    parent.path.write_text('\n'.join(parent.path.read_text().splitlines()[:2]) + '\n')
  else:
    values = entries(parent.path)
    values[0]['forked_from'] = {'run': 'child', 'step': 0}
    parent.path.write_text(''.join(json.dumps(value) + '\n' for value in values))
  with pytest.raises(RunLogError) as raised:
    run_log.chain(run_log.read(child.path), 0)
  assert 'child.jsonl' in str(raised.value)
  if missing != 'cycle':
    assert 'parent.jsonl' in str(raised.value)


def test_continuable_skips_only_start_debris_and_refuses_older_formats(tmp_path):
  oldest = started(tmp_path / '001.jsonl')
  oldest.step('user_input', 'hello')
  middle = started(tmp_path / '002.jsonl')
  middle.path.write_text(middle.path.read_text().splitlines()[0] + '\n')
  (tmp_path / '003.jsonl').write_text('{"kind":"open"')
  assert run_log.continuable(tmp_path) == oldest.path
  values = entries(oldest.path)
  values[0]['format'] = 1
  oldest.path.write_text(''.join(json.dumps(value) + '\n' for value in values))
  with pytest.raises(run_log.UnsupportedFormat, match=str(oldest.path)):
    run_log.continuable(tmp_path)
