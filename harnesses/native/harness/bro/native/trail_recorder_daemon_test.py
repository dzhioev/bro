import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import textwrap

import pytest

from bro.base import credentials, spawn
from bro.monitor import health, trail_pointer
from bro.native import trail_recorder
from bro.native.run_log_test_helper import open_log, session_environment
from bro.trails.store import default_store


@contextlib.contextmanager
def pipe():
  reader, writer = os.pipe()
  with os.fdopen(reader, 'rb', buffering=0) as source, os.fdopen(writer, 'wb', buffering=0) as sink:
    yield source, sink


@contextlib.contextmanager
def running(argv, **options):
  process = spawn.popen(argv, **options)
  try:
    yield process
  finally:
    process.kill()
    process.wait()


@contextlib.contextmanager
def orphan(pid):
  try:
    yield
  except BaseException:
    with contextlib.suppress(ProcessLookupError):
      os.kill(pid, signal.SIGKILL)
    raise


def readable(stream):
  assert select.select([stream], [], [], 15)[0], 'timed out waiting for recorder signal'


@pytest.fixture
def environment(tmp_path, monkeypatch):
  value = session_environment(tmp_path)
  for key, item in value.items():
    monkeypatch.setenv(key, item)
  return value


def test_ready_precedes_blocked_store_and_immediate_stop_runs_final_pass(tmp_path, environment):
  path = tmp_path / 'runs' / 'run.jsonl'
  writer = open_log(path)
  writer.step('user_input', 'record the whole log')
  writer.end('ok')
  program = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from bro.native import trail_recorder
    original = trail_recorder.default_store
    def blocked_store():
      os.write(int(sys.argv[2]), b's')
      os.read(int(sys.argv[3]), 1)
      return original()
    trail_recorder.default_store = blocked_store
    raise SystemExit(trail_recorder.record_session(Path(sys.argv[4]), Path(sys.argv[5]), int(sys.argv[1]), interval=0.05))
  """)
  with (
    pipe() as (ready, ready_writer),
    pipe() as (entered, entered_writer),
    pipe() as (release, release_writer),
  ):
    with running(
      [
        sys.executable,
        '-c',
        program,
        str(ready_writer.fileno()),
        str(entered_writer.fileno()),
        str(release.fileno()),
        str(path),
        str(tmp_path / 'recorder.log'),
      ],
      env=environment,
      pass_fds=(ready_writer.fileno(), entered_writer.fileno(), release.fileno()),
    ) as process:
      ready_writer.close()
      entered_writer.close()
      release.close()
      readable(ready)
      assert ready.read(1) == b'1'
      process.send_signal(signal.SIGTERM)
      readable(entered)
      assert entered.read(1) == b's'
      release_writer.write(b'1')
      assert process.wait(timeout=15) == 0
  pointer = trail_pointer.path()
  assert pointer is not None
  identifier = trail_pointer.read(pointer)
  assert identifier is not None
  with default_store() as store:
    assert len(list(store.iter_steps(identifier))) == 2
    assert store.get_trail(identifier)['end']['reason'] == 'ok'
  health_path = health.health_path()
  assert health_path is not None
  result = json.loads(health_path.read_text())
  assert result['status'] == 'ok'
  assert result['stale_after'] is None


def test_missing_selected_credential_beats_error_after_readiness(
  tmp_path, environment, monkeypatch
):
  def missing():
    raise credentials.SecretNotFound('trails')

  monkeypatch.setattr(trail_recorder, 'default_store', missing)
  with pipe() as (ready, writer):
    descriptor = os.dup(writer.fileno())
    assert (
      trail_recorder.record_session(tmp_path / 'run.jsonl', tmp_path / 'recorder.log', descriptor)
      == 1
    )
    readable(ready)
    assert ready.read(1) == b'1'
  health_path = health.health_path()
  assert health_path is not None
  result = json.loads(health_path.read_text())
  assert result['status'] == 'error'
  assert result['error'] == 'config not found: trails'
  assert result['stale_after'] is None


def test_parent_exit_causes_final_pass(tmp_path, environment):
  path = tmp_path / 'runs' / 'run.jsonl'
  log_path = tmp_path / 'recorder.log'
  program = textwrap.dedent("""
    import os, sys
    from bro.base import spawn
    reader, writer = os.pipe()
    child = spawn.popen([spawn.console_script('bro.native.trail-recorder'), '--run-log', sys.argv[3], '--log-path', sys.argv[4], '--ready-fd', str(writer), '--interval', '0.05'], pass_fds=(writer,))
    os.close(writer)
    assert os.read(reader, 1) == b'1'
    os.write(int(sys.argv[1]), (str(child.pid) + '\\n').encode())
    os.read(int(sys.argv[2]), 1)
    os._exit(0)
  """)
  with (
    pipe() as (ready, ready_writer),
    pipe() as (release, release_writer),
    log_path.open('ab') as output,
  ):
    with running(
      [
        sys.executable,
        '-c',
        program,
        str(ready_writer.fileno()),
        str(release.fileno()),
        str(path),
        str(log_path),
      ],
      env=environment,
      pass_fds=(ready_writer.fileno(), release.fileno()),
      stdout=subprocess.PIPE,
      stderr=output,
    ) as parent:
      ready_writer.close()
      release.close()
      readable(ready)
      child_pid = int(ready.readline())
      with orphan(child_pid):
        writer = open_log(path)
        writer.step('user_input', 'before parent exit')
        writer.end('ok')
        release_writer.write(b'1')
        assert parent.wait(timeout=15) == 0
        assert parent.stdout is not None
        readable(parent.stdout)
        assert parent.stdout.read(1) == b''
  pointer = trail_pointer.path()
  assert pointer is not None
  identifier = trail_pointer.read(pointer)
  assert identifier is not None
  with default_store() as store:
    assert store.get_trail(identifier)['end']['reason'] == 'ok'
    assert len(list(store.iter_steps(identifier))) == 2
  health_path = health.health_path()
  assert health_path is not None
  assert json.loads(health_path.read_text())['stale_after'] is None
