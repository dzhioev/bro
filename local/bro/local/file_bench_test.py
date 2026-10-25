import io
import json
import subprocess
import urllib.error
from dataclasses import replace
from email.message import Message
from unittest.mock import AsyncMock

import pytest

from bro.local import file_bench


def records(capsys):
  return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_public_input_fetch_verifies_then_reuses_cache(tmp_path, monkeypatch):
  data = b'public document bytes'
  source = replace(
    file_bench.PUBLIC_INPUTS['paper'], sha256=file_bench.hashlib.sha256(data).hexdigest()
  )
  monkeypatch.setitem(file_bench.PUBLIC_INPUTS, 'paper', source)
  requests = []

  def download(url, **arguments):
    requests.append(url)
    return io.BytesIO(data)

  monkeypatch.setattr(file_bench.urllib.request, 'urlopen', download)
  path = file_bench.public_input('paper', tmp_path)
  assert path is not None and path.read_bytes() == data
  assert file_bench.public_input('paper', tmp_path) == path
  assert requests == [source.url]
  path.write_bytes(b'corrupt cache')
  with pytest.raises(ValueError, match='SHA-256 mismatch'):
    file_bench.public_input('paper', tmp_path)
  path.unlink()
  monkeypatch.setitem(file_bench.PUBLIC_INPUTS, 'paper', replace(source, sha256='0' * 64))
  with pytest.raises(ValueError, match='SHA-256 mismatch'):
    file_bench.public_input('paper', tmp_path)
  assert not path.exists()


def test_public_input_skips_unavailable_network_but_not_broken_pins(tmp_path, monkeypatch):
  def offline(*arguments, **keywords):
    raise urllib.error.URLError('network unreachable')

  monkeypatch.setattr(file_bench.urllib.request, 'urlopen', offline)
  assert file_bench.public_input('paper', tmp_path) is None

  def missing(*arguments, **keywords):
    raise urllib.error.HTTPError('https://example.test', 404, 'missing', Message(), None)

  monkeypatch.setattr(file_bench.urllib.request, 'urlopen', missing)
  with pytest.raises(urllib.error.HTTPError):
    file_bench.public_input('paper', tmp_path)


@pytest.mark.asyncio
async def test_offline_skips_only_network_cells(tmp_path, monkeypatch, capsys):
  def network(*arguments):
    pytest.fail('offline benchmark accessed the network')

  monkeypatch.setattr(file_bench, 'public_input', network)
  calls = []

  async def generated(directory, public, report):
    assert directory.is_dir()
    assert public == {}
    calls.append('speed')

  async def fidelity(directory, report):
    calls.append('fidelity')

  monkeypatch.setattr(file_bench, 'speed', generated)
  monkeypatch.setattr(file_bench, 'fidelity', fidelity)
  monkeypatch.setattr(file_bench, 'security', lambda report: calls.append('security'))
  monkeypatch.setattr(
    file_bench, 'tokens', AsyncMock(side_effect=AssertionError('tokens ran offline'))
  )
  assert await file_bench.benchmark(list(file_bench.CELLS), True, tmp_path)
  assert calls == ['speed', 'security', 'fidelity']
  assert all(record['status'] == 'skipped' for record in records(capsys))


def test_gate_failure_is_sticky_and_boundary_is_inclusive(capsys):
  report = file_bench.Report()
  report.gate('boundary', 1, 1, 'seconds')
  assert not report.failed
  report.gate('over', 1.001, 1, 'seconds')
  report.record('fidelity', 'following case')
  assert report.failed
  assert [record['status'] for record in records(capsys)] == ['ok', 'failed', 'ok']


@pytest.mark.parametrize('returncode', [0, 1])
def test_security_runs_confinement_and_propagates_failure(monkeypatch, capsys, returncode):
  def run(command, **arguments):
    assert command[:3] == [file_bench.sys.executable, '-m', 'pytest']
    assert 'bro/files/confinement_sandbox_test.py' in command
    assert arguments['cwd'] == file_bench.ROOT
    return subprocess.CompletedProcess(command, returncode, 'suite output', 'suite error')

  monkeypatch.setattr(file_bench.subprocess, 'run', run)
  report = file_bench.Report()
  file_bench.security(report)
  assert report.failed == bool(returncode)
  output = records(capsys)
  assert output[0]['output'] == 'suite outputsuite error'
  assert output[1]['status'] == 'deferred'


def test_main_exits_nonzero_when_a_selected_cell_fails(tmp_path, monkeypatch):
  async def failed(cells, offline, cache):
    assert cells == ['fidelity']
    assert offline and cache == tmp_path
    return False

  monkeypatch.setattr(file_bench, 'benchmark', failed)
  with pytest.raises(SystemExit) as error:
    file_bench.main(['file-bench', '--cell', 'fidelity', '--offline', '--cache', str(tmp_path)])
  assert error.value.code == 1


@pytest.mark.asyncio
async def test_each_measurement_owns_a_fresh_operations_state(tmp_path, monkeypatch):
  states = []

  class Operations:
    def __init__(self):
      self.closed = False
      states.append(self)

    async def info(self, path):
      assert path == str(tmp_path)
      return self

    async def view(self, path, view):
      assert view == 'text'
      return await self.info(path)

    def model_dump(self):
      assert self.closed
      return {'result': 'measured'}

    def close(self):
      self.closed = True

  monkeypatch.setattr(file_bench, 'Operations', Operations)
  times = iter([10, 12, 20, 23])
  cpu = iter([1, 1.5, 2, 2.75])
  monkeypatch.setattr(file_bench.time, 'perf_counter', lambda: next(times))
  monkeypatch.setattr(file_bench, 'cpu_seconds', lambda: next(cpu))
  assert await file_bench.measure(tmp_path, None) == ({'result': 'measured'}, 2, 0.5)
  assert await file_bench.measure(tmp_path, 'text') == ({'result': 'measured'}, 3, 0.75)
  assert len(states) == 2 and all(state.closed for state in states)


@pytest.mark.asyncio
async def test_tokens_network_unavailable_is_not_a_failure(tmp_path, monkeypatch, capsys):
  import tiktoken
  from requests.exceptions import ConnectionError

  def unavailable(name):
    raise ConnectionError('unreachable')

  monkeypatch.setattr(tiktoken, 'get_encoding', unavailable)
  report = file_bench.Report()
  await file_bench.tokens(tmp_path, {}, report)
  assert not report.failed
  assert records(capsys)[0]['status'] == 'skipped'


@pytest.mark.asyncio
@pytest.mark.parametrize('duration', [0, 1000])
async def test_speed_enforces_all_four_numeric_gates(tmp_path, monkeypatch, capsys, duration):
  _, text = file_bench.dense_page()

  async def measure(path, view):
    if path.name == 'dense.pdf':
      result = {'content': {'pages': [{'text': text, 'notes': []}] * 330}}
    elif path.name == '5000-pages.pdf':
      result = {'details': {'page_count': 5000, 'without_text_page_count': 3000}}
    elif path.name == 'paper.pdf':
      result = (
        {'details': {'page_count': 15}} if view is None else {'content': {'pages': [{}] * 15}}
      )
    else:
      result = {}
    return result, duration, duration

  monkeypatch.setattr(file_bench, 'measure', measure)
  report = file_bench.Report()
  await file_bench.speed(tmp_path, {'paper': tmp_path / 'paper.pdf'}, report)
  assert report.failed == bool(duration)
  gates = [record for record in records(capsys) if 'maximum' in record]
  assert len(gates) == 4
  assert all(record['status'] == ('failed' if duration else 'ok') for record in gates)
